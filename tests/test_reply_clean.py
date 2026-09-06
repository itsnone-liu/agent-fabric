"""结果正文提取测试：过程剥净、正文（数字/代码块/货币符号）保全。"""
from __future__ import annotations

from fabric.central.reply_clean import extract_reply

RAW1 = """\x1b[0m
> build · mimo-v2.5-free
\x1b[0m\x1b[0m$ \x1b[0mpwd
/opt/agent-fabric/workspace
\x1b[0m
完成。当前工作目录是 `/opt/agent-fabric/workspace`。"""

RAW2 = """\x1b[0m
> build · mimo-v2.5-free
\x1b[0m\x1b[0m← \x1b[0mWrite session_demo.txt
Wrote file successfully.
\x1b[0m\x1b[0m$ \x1b[0mcat session_demo.txt
会话化指挥第一枪
\x1b[0m
已创建 session_demo.txt 并确认内容：`会话化指挥第一枪`。"""


def test_basic_extraction():
    r = extract_reply(RAW1)
    assert r and "工作目录是" in r and "/opt/agent-fabric/workspace" in r
    assert "build" not in r and "$" not in r.split("\n")[0]
    r2 = extract_reply(RAW2)
    assert r2 and "已创建" in r2 and "Wrote file" not in r2
    assert "←" not in r2


def test_code_fence_and_symbols_preserved():
    raw = """\x1b[0m
> build · mimo-v2.5-free
\x1b[0m\x1b[0m$ \x1b[0mbash setup.sh
OK
\x1b[0m
部署完成，共 3 步，花费 $5。脚本如下：
```bash
$ pip install foo
# 依赖 123 个
echo "done"
```
总结：一切正常，得分 9.5/10。"""
    r = extract_reply(raw)
    assert r is not None
    assert "```bash" in r and "$ pip install foo" in r and "# 依赖 123 个" in r
    assert "花费 $5" in r and "9.5/10" in r
    assert "> build" not in r and "←" not in r


def test_too_short_falls_back_none():
    assert extract_reply("\x1b[0m\n> build · m\n$ x\n") is None


def test_multiline_answer_paragraphs_kept():
    raw = "$ ls\na b c\n第一段结论：文件齐全。\n\n第二段：建议保留 $PATH 环境变量。"
    r = extract_reply(raw)
    assert r and "第一段结论" in r and "第二段" in r and "$PATH" in r
    assert "$ ls" not in r
