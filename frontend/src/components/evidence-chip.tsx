"use client";

import { useState } from "react";
import { api } from "@/lib/api/client";

type Evidence = {
  id: string; source_type: string; source_url: string | null; locator: Record<string, unknown>;
  excerpt: string; content_hash: string; truncated: boolean; observed_at: string;
};

export default function EvidenceChip({ id }: { id: string }) {
  const [open, setOpen] = useState(false);
  const [data, setData] = useState<Evidence | null>(null);
  const [error, setError] = useState("");
  async function toggle() {
    if (!open && data === null) {
      try {
        setData(await api<Evidence>(`/evidence/${id}`));
      } catch (err) {
        setError(err instanceof Error ? err.message : "读取失败");
      }
    }
    setOpen(!open);
  }
  return (
    <span className="evidence-chip-wrap">
      <button className="chip chip-button" onClick={toggle} title="查看证据">
        证据 {id.slice(0, 8)}
      </button>
      {open && (
        <span className="evidence-pop">
          {error && <span className="error-inline">{error}</span>}
          {data && (
            <>
              <span className="evidence-line">
                <b>{data.source_type}</b> · {data.observed_at.slice(0, 19).replace("T", " ")}
              </span>
              {Object.entries(data.locator)
                .filter(([key]) => key !== "object_ref")
                .map(([key, value]) => (
                  <span key={key} className="evidence-line muted">
                    {key}: {String(value)}
                  </span>
                ))}
              <pre className="evidence-excerpt">{data.excerpt}{data.truncated ? "…" : ""}</pre>
              <span className="evidence-line muted">sha256 {data.content_hash.slice(0, 16)}…</span>
              {data.source_url && (
                <a className="link-button" href={data.source_url} target="_blank" rel="noreferrer">
                  在 GitHub 打开
                </a>
              )}
            </>
          )}
        </span>
      )}
    </span>
  );
}
