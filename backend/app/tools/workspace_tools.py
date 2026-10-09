"""Pinned-SHA code tools (README 9.2 read_file / search_code).

All reads happen at the run's fixed head SHA; the model chooses paths,
line ranges and queries only. Every result is captured as code evidence.
"""

import json

from app.domain.contracts import ToolResult
from app.tools.executor import ToolDeps, error_result, ok_result
from app.tools.github_tools import ReadFileInput, SearchCodeInput
from app.workspace.reader import read_file
from app.workspace.search import search_code

SEARCH_HIT_LINE_CHARS = 200
SEARCH_VISIBLE_HITS = 50


async def read_file_tool(deps: ToolDeps, args: ReadFileInput) -> ToolResult:
    sha = deps.ctx.head_sha
    if not sha:
        return error_result("NO_FIXED_SHA", "本次运行没有固定的代码版本", source="workspace")
    result = read_file(
        deps.settings,
        deps.workspace,
        deps.repo,
        sha,
        args.relative_path,
        start_line=args.start_line,
        end_line=args.end_line,
    )
    evidence_id = deps.evidence(
        source_type="code",
        content=result.text,
        locator={
            "commit_sha": sha,
            "path": result.path,
            "line_start": result.line_start,
            "line_end": result.line_end,
        },
        source_url=f"https://github.com/{deps.repo.full_name}/blob/{sha}/{result.path}",
    )
    return ok_result(
        {
            "path": result.path,
            "commit_sha": sha,
            "line_start": result.line_start,
            "line_end": result.line_end,
            "total_lines": result.total_lines,
            "text": result.text,
        },
        source="workspace",
        evidence_ids=[evidence_id],
        truncated=result.truncated,
    )


async def search_code_tool(deps: ToolDeps, args: SearchCodeInput) -> ToolResult:
    sha = deps.ctx.head_sha
    if not sha:
        return error_result("NO_FIXED_SHA", "本次运行没有固定的代码版本", source="workspace")
    results = search_code(
        deps.settings,
        deps.workspace,
        deps.repo,
        sha,
        args.query,
        path_glob=args.path_glob,
    )
    visible = results.hits[:SEARCH_VISIBLE_HITS]
    hits = [
        {
            "path": hit.path,
            "line_number": hit.line_number,
            "line": hit.line[:SEARCH_HIT_LINE_CHARS],
        }
        for hit in visible
    ]
    hit_dump = json.dumps(hits, ensure_ascii=False, indent=1)
    evidence_id = deps.evidence(
        source_type="code_search",
        content=f"query={args.query!r} path_glob={args.path_glob!r} sha={sha}\n{hit_dump}",
        locator={"commit_sha": sha, "query": args.query, "path_glob": args.path_glob},
    )
    return ok_result(
        {
            "commit_sha": sha,
            "query": args.query,
            "hits": hits,
            "total_matches": results.total_matches,
            "visible_hits": len(visible),
        },
        source="workspace",
        evidence_ids=[evidence_id],
        truncated=results.truncated or len(results.hits) > len(visible),
        next_cursor=results.next_cursor,
        warnings=["HITS_TRUNCATED"] if len(results.hits) > len(visible) else [],
    )
