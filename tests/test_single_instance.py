"""单实例锁：一台机器的同一个登录会话里只允许一个 PastePing。

**背景**：2026-10-07 用户实测 ``tasklist`` 时发现**同时有两个 ``PastePing.exe``
在跑**，它们共用同一个日志文件。两个托盘图标一模一样、两个监听器都在写剪贴板，
用户根本分不清在用哪一个。当时 ``main.py`` 里**没有任何单实例保护**。

**设计要点（本文件同时是它的护栏）**：

* 锁只加在真实入口 :func:`main.run_cli` 上，**不**加在 :func:`main.main` 里。
  ``main`` 是纯装配逻辑，同一个进程里要能被反复调用（``_run_main_harness`` 就是
  靠它跑起来的）；「只能有一个」是**进程级**约束，属于入口的职责。
  ``TestMainDoesNotOwnTheLock`` 专门钉住这条分工。
* 抢不到锁时返回 0 并**给用户一次看得见的回应** —— 用户的动作是双击图标，
  什么都不显示就和「没启动成功」一样。
"""

from __future__ import annotations

import ast
import itertools
import os
import subprocess
import sys

import pytest

import main

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_names = itertools.count()


def _unique_name() -> str:
    """每个用例用互斥量名互不干扰（否则用例之间会互相把对方锁在外面）。"""
    return f"Local\\PastePing.Test.{next(_names)}.{os.getpid()}"


class TestAcquireRelease:
    """锁本身的语义。"""

    def test_first_acquire_wins_second_is_rejected(self):
        """同进程内第二次拿同一把锁必须被拒 —— 这正是「两个实例」的判据。"""
        name = _unique_name()
        handle, first = main._acquire_single_instance(name)
        assert first is True
        assert handle, "抢到锁时应当返回有效句柄"
        try:
            second_handle, second = main._acquire_single_instance(name)
            try:
                assert second is False, "第二个实例必须被拒绝"
            finally:
                main._release_single_instance(second_handle)
        finally:
            main._release_single_instance(handle)

    def test_lock_is_freed_after_release(self):
        """释放后必须能重新拿到锁 —— 否则第一个实例退出后就再也起不来了。"""
        name = _unique_name()
        handle, first = main._acquire_single_instance(name)
        assert first is True
        main._release_single_instance(handle)

        handle2, first2 = main._acquire_single_instance(name)
        try:
            assert first2 is True, "释放后应当可以重新拿到锁"
        finally:
            main._release_single_instance(handle2)

    def test_release_tolerates_zero_handle(self):
        """没拿到锁时（句柄 0）释放必须是空操作，不能抛异常。"""
        main._release_single_instance(0)
        main._release_single_instance(None)


class TestCrossProcess:
    """真正要证明的是**跨进程**：同进程的判据不能替代跨进程的判据。"""

    def _run_child(self, name: str) -> str:
        """另起一个进程去抢同一把锁，返回它看到的结论。"""
        script = (
            "import sys; sys.path.insert(0, r'{}'); import main; "
            "h, first = main._acquire_single_instance(sys.argv[1]); "
            "print('FIRST' if first else 'BLOCKED')"
        ).format(_PROJECT_ROOT)
        proc = subprocess.run(
            [sys.executable, "-c", script, name],
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert proc.returncode == 0, f"子进程失败：{proc.stdout}\n{proc.stderr}"
        return proc.stdout.strip()

    def test_separate_process_is_rejected_while_held(self):
        """本进程持锁时，另一个**真进程**必须被拒。"""
        name = _unique_name()
        handle, first = main._acquire_single_instance(name)
        assert first is True
        try:
            assert self._run_child(name) == "BLOCKED"
        finally:
            main._release_single_instance(handle)

    def test_separate_process_succeeds_after_release(self):
        """锁释放后，另一个真进程必须能拿到 —— 证明锁不是永久占用的。"""
        name = _unique_name()
        handle, first = main._acquire_single_instance(name)
        assert first is True
        main._release_single_instance(handle)
        assert self._run_child(name) == "FIRST"


class TestRunCli:
    """入口的分支行为。"""

    def test_second_instance_exits_without_starting(self, monkeypatch):
        """被锁拦下时：给提示、返回 0、**绝不**启动托盘程序。"""
        seen = {"report": 0, "main": 0}
        monkeypatch.setattr(main, "_acquire_single_instance", lambda *a, **k: (0, False))
        monkeypatch.setattr(
            main, "_report_already_running", lambda: seen.__setitem__("report", seen["report"] + 1)
        )
        monkeypatch.setattr(
            main, "main", lambda: (seen.__setitem__("main", seen["main"] + 1), 0)[1]
        )

        assert main.run_cli() == 0
        assert seen["report"] == 1, "必须告诉用户为什么没启动（双击后毫无动静最糟）"
        assert seen["main"] == 0, "被锁拦下时绝不能启动第二个托盘程序"

    def test_first_instance_starts_and_releases_the_lock(self, monkeypatch):
        released = []
        monkeypatch.setattr(main, "_acquire_single_instance", lambda *a, **k: (4242, True))
        monkeypatch.setattr(main, "_release_single_instance", released.append)
        monkeypatch.setattr(main, "main", lambda: 7)

        assert main.run_cli() == 7
        assert released == [4242], "正常退出时应当释放锁"

    def test_lock_is_released_even_when_startup_raises(self, monkeypatch):
        """启动过程抛异常也必须放锁，否则本次崩溃会连带锁住下一次启动。"""
        released = []
        monkeypatch.setattr(main, "_acquire_single_instance", lambda *a, **k: (4242, True))
        monkeypatch.setattr(main, "_release_single_instance", released.append)

        def _boom():
            raise RuntimeError("启动失败")

        monkeypatch.setattr(main, "main", _boom)

        with pytest.raises(RuntimeError):
            main.run_cli()
        assert released == [4242]


class TestMainDoesNotOwnTheLock:
    """钉住分工：``main()`` 是纯装配，不得自己抢锁。"""

    def test_main_runs_while_lock_is_already_held(self, monkeypatch):
        """本进程已持锁时，``main()`` 仍必须能照常装配起来。

        这条不是多余的：万一有人把抢锁挪进 ``main()``，测试里第二次调用 ``main()``
        就会**静默提前退出**，而既有用例只会报「未能捕获 handle_text」，
        很难一眼看出根因。这里让失败直接指向原因。
        """
        from test_qa_v02_adversarial import _run_main_harness

        handle, first = main._acquire_single_instance(_unique_name())
        assert first is True
        try:
            captured = _run_main_harness(monkeypatch)
        finally:
            main._release_single_instance(handle)

        assert captured["on_text"] is not None, "持锁状态下 main() 不该被挡住"


class TestEntryPointWiring:
    """``python main.py`` / exe 必须真的走 ``run_cli``，否则锁形同虚设。"""

    def _entry_block(self):
        with open(os.path.join(_PROJECT_ROOT, "main.py"), encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        blocks = [
            node
            for node in tree.body
            if isinstance(node, ast.If) and "__main__" in ast.dump(node.test)
        ]
        assert blocks, "main.py 缺少 `if __name__ == '__main__'` 入口块"
        return blocks[0]

    def test_entry_block_calls_run_cli(self):
        called = {
            node.func.id
            for node in ast.walk(self._entry_block())
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert "run_cli" in called, "入口块必须调用 run_cli() —— 单实例锁在那里"

    def test_entry_block_does_not_call_main_directly(self):
        """直接调 ``main()`` 会**绕过**锁，必须禁止（这是最容易犯的回退）。"""
        called = {
            node.func.id
            for node in ast.walk(self._entry_block())
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert "main" not in called, "入口块直接调 main() 会绕过单实例锁"
