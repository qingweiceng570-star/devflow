import assert from "node:assert/strict";
import test from "node:test";

import {
  FORMAT_REPOSITORY_MESSAGE,
  HOST_REPOSITORY_MESSAGE,
  INCOMPLETE_REPOSITORY_MESSAGE,
  normalizeGithubRepository,
  parseGithubRepository,
} from "../../src/lib/github-repository.ts";

test("accepts repository pages, sub-pages and clone forms", () => {
  const accepted = [
    "https://github.com/octo/demo",
    "https://www.github.com/octo/demo.git?tab=readme#readme",
    "github.com/octo/demo/tree/main",
    "git@github.com:octo/demo.git",
    "ssh://git@github.com/octo/demo.git",
    "ssh://git@github.com:22/octo/demo.git",
    "git://github.com/octo/demo.git",
    "git clone https://github.com/octo/demo.git",
    "git clone git@github.com:octo/demo.git",
    "  'https://github.com/octo/demo'  ",
    "https://github.com/octo/demo。",
    "https://github.com/octo/demo（推荐）",
    "https://github.com//octo//demo",
    "HTTPS://GITHUB.COM/octo/demo",
    "octo/demo",
  ];
  for (const value of accepted) {
    assert.equal(normalizeGithubRepository(value), "octo/demo", value);
  }
});

test("rejects non-GitHub and incomplete links with a specific reason", () => {
  assert.equal(parseGithubRepository("https://gitlab.com/octo/demo").ok, false);
  assert.equal(
    parseGithubRepository("https://gitlab.com/octo/demo").message,
    HOST_REPOSITORY_MESSAGE,
  );
  assert.equal(parseGithubRepository("https://github.com/octo").message, INCOMPLETE_REPOSITORY_MESSAGE);
  assert.equal(parseGithubRepository("https://github.com/octo/%ZZ").message, FORMAT_REPOSITORY_MESSAGE);
  assert.equal(parseGithubRepository("").ok, false);
});
