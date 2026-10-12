"""内嵌报告归档：把助手消息中以 REPORT_HTML_START/END 包裹的 HTML 看板落盘为工作区产物。

数据分析 / 市场简报等员工会把完整 HTML 看板直接内联在回复文本里。本模块负责：
- 从助手消息中提取合法可渲染的 HTML 看板（REPORT_HTML_START/END 分隔符）；
- 去重后写入受控的私有产物目录（按 user/会话哈希隔离），登记 conversation_files；
- 历史会话回读时幂等补登记（幂等幂等，只归档 AIMessage，忽略工具消息）。
"""

from __future__ import annotations

import hashlib
import html
import logging
import os
import re
import time
from html.parser import HTMLParser
from pathlib import Path

from app import conversations
from app.artifact_storage import MAX_ARTIFACT_SIZE
from app.paths import WORKSPACE_DATA

logger = logging.getLogger("app.report_artifacts")

_REPORT_RE = re.compile(
    r"<!--\s*REPORT_HTML_START\s*-->([\s\S]*?)<!--\s*REPORT_HTML_END\s*-->",
    re.IGNORECASE,
)


class _TitleParser(HTMLParser):
    """极简 HTML 解析器：只抓取 <title> 标签内的文本，用于给看板起文件名。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.in_title = False
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "title":
            self.in_title = True

    def handle_endtag(self, tag):
        if tag.lower() == "title":
            self.in_title = False

    def handle_data(self, data):
        if self.in_title:
            self.parts.append(data)


def _is_renderable_report(value: str) -> bool:
    """判断字符串是否是「可渲染的完整 HTML 文档」，避免把短片段误归档成看板。"""
    source = (value or "").strip()
    if len(source) < 80:
        return False
    return bool(
        re.search(r"<!doctype\s+html", source, re.IGNORECASE)
        or (re.search(r"<html[\s>]", source, re.IGNORECASE)
            and re.search(r"</html\s*>", source, re.IGNORECASE))
        or (re.search(r"<(?:head|body|div|section|main|script|style|canvas|svg)[\s>]",
                      source, re.IGNORECASE)
            and re.search(r"</(?:div|section|main|script|style|canvas|svg)\s*>",
                          source, re.IGNORECASE))
    )


def extract_inline_reports(content: str) -> list[str]:
    """从助手回复中提取所有合法的 REPORT_HTML 看板块，并按内容哈希去重。

    兼容裸 HTML 与 ```html 代码块包裹两种写法，剔除过短或非完整文档的片段。
    """
    if not isinstance(content, str) or not content:
        return []
    found: list[str] = []
    seen: set[str] = set()
    for match in _REPORT_RE.finditer(content):
        report = match.group(1).strip()
        fence = re.fullmatch(r"```(?:html)?\s*\n([\s\S]*?)\n```", report, re.IGNORECASE)
        if fence:
            report = fence.group(1).strip()
        if not _is_renderable_report(report):
            continue
        digest = hashlib.sha256(report.encode("utf-8")).hexdigest()
        if digest not in seen:
            found.append(report)
            seen.add(digest)
    return found


def _report_title(report: str) -> str:
    """提取看板 <title> 作为文件名主体；无标题或提取失败时回退「数据分析报告」。"""
    parser = _TitleParser()
    try:
        parser.feed(report[:200_000])
    except Exception:
        return "数据分析报告"
    title = re.sub(r"\s+", " ", html.unescape("".join(parser.parts))).strip()
    title = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", title).strip(" ._")
    return (title[:72] or "数据分析报告")


def save_inline_report(workspace_root: Path, user_id: str, conv_id: str,
                       turn_no: int | None, report: str) -> dict | None:
    """把内嵌看板写入私有产物目录（稳定路径 + 原子写入），返回登记信息。

    稳定路径 = 内容哈希去重，同一看板重复出现不重写；写入用临时文件 + os.replace
    原子替换，避免并发读到半成品。
    """
    if not _is_renderable_report(report):
        return None
    payload = report.encode("utf-8")
    if len(payload) > MAX_ARTIFACT_SIZE:
        logger.warning("内嵌看板超过产物大小限制，跳过归档 conv=%s size=%d", conv_id, len(payload))
        return None

    root = Path(workspace_root).resolve()
    user_key = hashlib.sha256((user_id or "default").encode()).hexdigest()[:24]
    conv_key = hashlib.sha256(conv_id.encode()).hexdigest()[:24]
    digest = hashlib.sha256(payload).hexdigest()
    turn_key = str(turn_no) if turn_no is not None else "unknown"
    name = f"{_report_title(report)}-T{turn_key}-{digest[:10]}.html"
    archive_dir = root / ".artifact-store" / user_key / conv_key / "inline-reports"
    target = archive_dir / name
    try:
        if any(part.is_symlink() for part in (
            root / ".artifact-store", root / ".artifact-store" / user_key,
            root / ".artifact-store" / user_key / conv_key, archive_dir,
        )):
            logger.warning("内嵌看板归档目录包含符号链接，跳过 conv=%s", conv_id)
            return None
        archive_dir.mkdir(parents=True, exist_ok=True)
        resolved_dir = archive_dir.resolve()
        if root not in resolved_dir.parents or target.is_symlink():
            logger.warning("内嵌看板归档路径越界，跳过 conv=%s", conv_id)
            return None
        if not target.is_file() or target.stat().st_size != len(payload):
            temp = archive_dir / f".{digest}.{time.time_ns()}.tmp"
            try:
                with temp.open("xb") as out:
                    out.write(payload)
                os.replace(temp, target)
            finally:
                temp.unlink(missing_ok=True)
        return {"name": name, "path": target.relative_to(root).as_posix(),
                "size": target.stat().st_size, "turn_no": turn_no}
    except OSError as exc:
        logger.warning("内嵌看板归档失败 conv=%s (%s)", conv_id, exc)
        return None


def archive_inline_reports(content: str, user_id: str, conv_id: str,
                           turn_no: int | None) -> list[dict]:
    """把单条助手回复中的看板全部归档并登记 conversation_files。"""
    saved: list[dict] = []
    for report in extract_inline_reports(content):
        item = save_inline_report(WORKSPACE_DATA, user_id, conv_id, turn_no, report)
        if not item:
            continue
        artifact_id = conversations.add_file(
            conv_id, item["name"], item["path"], item["size"], turn_no,
            artifact_type="inline_report",
        )
        if artifact_id:
            saved.append({**item, "artifact_id": artifact_id,
                          "artifact_type": "inline_report"})
    return saved


def _message_text(message) -> str:
    """取消息的纯文本内容（兼容字符串 content 与多段 list content）。"""
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content
                       if isinstance(part, dict) and part.get("type") == "text")
    return ""


def archive_assistant_reports(messages: list, user_id: str,
                              conv_id: str) -> list[dict]:
    """从历史会话的已持久化消息里幂等回填看板（仅 AIMessage，忽略工具消息）。

    供打开历史对话时补登记：工具消息里会回显技能模板或文件内容，若一并归档
    会产生大量噪音，因此只归档面向用户的助手回答。
    """
    turn_no = 0
    saved: list[dict] = []
    seen_artifact_ids: set[int] = set()
    for message in messages or []:
        kind = type(message).__name__
        if kind == "HumanMessage":
            turn_no += 1
            continue
        if kind != "AIMessage":
            # ToolMessage bodies can echo a file or a skill template. Only the
            # user-facing assistant answer should become a workspace artifact.
            continue
        for artifact in archive_inline_reports(_message_text(message), user_id,
                                              conv_id, turn_no or None):
            artifact_id = artifact.get("artifact_id")
            if artifact_id not in seen_artifact_ids:
                saved.append(artifact)
                if artifact_id is not None:
                    seen_artifact_ids.add(artifact_id)
    return saved
