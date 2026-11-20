const REPOSITORY_PART = /^[A-Za-z0-9_.-]{1,100}$/;
const GITHUB_HOSTS = new Set(["github.com", "www.github.com"]);
const CLONE_PREFIX = /^git\s+clone\s+/i;
// ``git@github.com:owner/repo`` and ``ssh://git@github.com/owner/repo`` are
// the two clone forms GitHub shows on the repository page.
const SCP_PREFIX = /^(?:ssh:\/\/)?git@github\.com(?::\d{1,5})?[:/]/i;
const BARE_HOST = /^(?:www\.)?github\.com\/+/i;
// Text pasted from chats and documents often keeps trailing punctuation or a
// wrapping quote; none of these characters can be part of a repository name.
const TRAILING_NOISE = /[。，、；：！？）】》”’"'`,;)}\]>]+$/u;
// A link pasted from an article often ends with a short parenthetical comment.
const TRAILING_COMMENT = /\s*[（(][^（）()]{0,60}[）)]\s*$/u;
const WRAPPING_QUOTES = /^["'`“”‘’]+|["'`“”‘’]+$/g;

export const EMPTY_REPOSITORY_MESSAGE = "请输入 GitHub 仓库链接，例如 https://github.com/owner/repo";
export const HOST_REPOSITORY_MESSAGE = "仅支持 github.com 仓库链接，请检查链接域名";
export const INCOMPLETE_REPOSITORY_MESSAGE = "请提供完整的 GitHub 仓库链接，例如 https://github.com/owner/repo";
export const FORMAT_REPOSITORY_MESSAGE = "GitHub 仓库格式不正确，请使用 owner/repo 或仓库链接";

export type GithubRepositoryParse =
  | { ok: true; fullName: string }
  | { ok: false; message: string };

/** Parse the repository values accepted by the import form. */
export function parseGithubRepository(value: string): GithubRepositoryParse {
  let raw = (value || "").trim().replace(WRAPPING_QUOTES, "").trim();
  raw = raw.replace(CLONE_PREFIX, "").trim();
  raw = raw.replace(TRAILING_COMMENT, "").trim();
  raw = raw.replace(TRAILING_NOISE, "").trim();
  if (!raw) return { ok: false, message: EMPTY_REPOSITORY_MESSAGE };

  let path = raw;
  if (SCP_PREFIX.test(raw)) {
    path = raw.replace(SCP_PREFIX, "");
  } else if (raw.includes("://")) {
    let parsed: URL;
    try {
      parsed = new URL(raw);
    } catch {
      return { ok: false, message: FORMAT_REPOSITORY_MESSAGE };
    }
    const scheme = parsed.protocol.replace(":", "").toLowerCase();
    const host = parsed.hostname.toLowerCase().replace(/\.$/, "");
    if (scheme === "http" || scheme === "https") {
      if (!GITHUB_HOSTS.has(host)) return { ok: false, message: HOST_REPOSITORY_MESSAGE };
      if (parsed.password || (parsed.username && parsed.username !== "git")) {
        return { ok: false, message: FORMAT_REPOSITORY_MESSAGE };
      }
      if (parsed.port && !["80", "443"].includes(parsed.port)) {
        return { ok: false, message: FORMAT_REPOSITORY_MESSAGE };
      }
    } else if (scheme === "ssh" || scheme === "git") {
      if (host !== "github.com") return { ok: false, message: HOST_REPOSITORY_MESSAGE };
      if (parsed.password || (parsed.username && parsed.username !== "git")) {
        return { ok: false, message: FORMAT_REPOSITORY_MESSAGE };
      }
      if (parsed.port && parsed.port !== "22") {
        return { ok: false, message: FORMAT_REPOSITORY_MESSAGE };
      }
    } else {
      return { ok: false, message: HOST_REPOSITORY_MESSAGE };
    }
    path = parsed.pathname;
  } else if (BARE_HOST.test(raw)) {
    path = raw.replace(BARE_HOST, "");
  }

  let decodedPath: string;
  try {
    decodedPath = decodeURIComponent(path);
  } catch {
    return { ok: false, message: FORMAT_REPOSITORY_MESSAGE };
  }
  const parts = decodedPath.replace(/^\/+|\/+$/g, "").split("/").filter(Boolean);
  if (parts.length < 2) return { ok: false, message: INCOMPLETE_REPOSITORY_MESSAGE };
  const owner = parts[0];
  const repository = parts[1].replace(/\.git$/i, "");
  if (!REPOSITORY_PART.test(owner) || !REPOSITORY_PART.test(repository)) {
    return { ok: false, message: FORMAT_REPOSITORY_MESSAGE };
  }
  return { ok: true, fullName: `${owner}/${repository}` };
}

/** Normalize the repository values accepted by the import form. */
export function normalizeGithubRepository(value: string): string | null {
  const parsed = parseGithubRepository(value);
  return parsed.ok ? parsed.fullName : null;
}
