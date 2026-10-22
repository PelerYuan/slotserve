"""Results of a verification run and the Markdown / JSON report built from them."""

import json
import platform
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

PASS, FAIL, SKIP, PENDING, MANUAL = "pass", "fail", "skip", "pending", "manual"
ICONS = {PASS: "PASS", FAIL: "FAIL", SKIP: "skip", PENDING: "pending", MANUAL: "manual"}


@dataclass
class Result:
    id: str  # stable, e.g. devenv.template.pytorch
    title: str
    status: str = PASS
    detail: str = ""  # one line for the table
    log: list[str] = field(default_factory=list)  # what happened, for the person reading the report
    seconds: float = 0.0

    def note(self, line: str) -> None:
        self.log.append(f"[{time.strftime('%H:%M:%S')}] {line}")


@dataclass
class Report:
    target: str
    server_version: str = "?"
    host: str = ""
    tool: str = "tools/verify 1"
    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds"))
    finished_at: str = ""
    runner: str = field(default_factory=lambda: f"{platform.node()} ({platform.system()} {platform.release()})")
    options: dict = field(default_factory=dict)
    results: list[Result] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        out = {s: 0 for s in ICONS}
        for r in self.results:
            out[r.status] = out.get(r.status, 0) + 1
        return out

    @property
    def failed(self) -> bool:
        return any(r.status == FAIL for r in self.results)

    def stem(self) -> str:
        def safe(text: str) -> str:
            return "".join(c if c.isalnum() or c in "-." else "-" for c in text)

        version = self.server_version if self.server_version not in ("", "?") else "unknown"
        return f"verify-report-{safe(self.host or 'unknown')}-{safe(version)}"

    def to_json(self) -> str:
        return json.dumps({**asdict(self), "counts": self.counts()}, indent=2, ensure_ascii=False)

    def to_markdown(self) -> str:
        c = self.counts()
        lines = [
            f"# SlotServe verification report: {self.host} {self.server_version}",
            "",
            f"- Target: `{self.target}`",
            f"- Server version: `{self.server_version}`",
            f"- Run from: {self.runner}",
            f"- Started: {self.started_at}; finished: {self.finished_at}",
            f"- Tool: {self.tool}; options: `{json.dumps(self.options, sort_keys=True)}`",
            f"- Result: **{c[PASS]} pass, {c[FAIL]} fail**, {c[SKIP]} skipped, {c[PENDING]} pending, "
            f"{c[MANUAL]} manual",
            "",
            "Statuses: *pending* = the server does not have the feature yet (the check runs once it does); "
            "*manual* = a person has to do this step (instructions below); *skip* = not applicable here or "
            "switched off (reason given).",
            "",
            "| Check | Status | Time | Detail |",
            "|---|---|---|---|",
        ]
        for r in self.results:
            detail = r.detail.replace("|", "\\|").replace("\n", " ")
            lines.append(f"| `{r.id}` {r.title} | {ICONS[r.status]} | {r.seconds:.0f} s | {detail} |")
        lines += ["", "## Details", ""]
        for r in self.results:
            lines += [f"### `{r.id}`: {ICONS[r.status]}", "", r.detail or "-", ""]
            if r.log:
                lines += ["```text", *r.log[-60:], "```", ""]
        return "\n".join(lines) + "\n"

    def write(self, out_dir: Path) -> tuple[Path, Path]:
        out_dir.mkdir(parents=True, exist_ok=True)
        md = out_dir / f"{self.stem()}.md"
        js = out_dir / f"{self.stem()}.json"
        md.write_text(self.to_markdown(), encoding="utf-8")
        js.write_text(self.to_json(), encoding="utf-8")
        return md, js
