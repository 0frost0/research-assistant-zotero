"""Prepare and publish retained plugin builds; never fabricate historical backends.

prepare is local/read-only except for new output directories. publish uses the
existing Git credential helper without printing or persisting its credential.
Existing releases/assets are never replaced; mismatches fail explicitly.
"""
import argparse
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
REPO = "0frost0/research-assistant-zotero"
API = "https://api.github.com"
FULL_SOURCE_VERSIONS = {"0.3.11", "0.3.12"}
WITHDRAWN_XPI_VERSIONS = {f"0.3.{patch}" for patch in range(14)}
NOTES = {
    "0.1.0": "初版桥接：manifest、bootstrap 与 bridge；仅归档实际留存插件。",
    "0.2.0": "分类知识库工作台、文献与笔记同步。保留已发布 research release 的原始包；local-retained 为同版本不同字节的本地留存变体。",
    "0.3.0": "个人文献记忆、异步问答历史、反馈、笔记 revision 与本机服务设置的累积开发版本。",
    "0.3.1": "版本递增用于区分旧设置窗口；与留存 0.3.0 包相比，业务资产无变化（manifest 除外）。",
    "0.3.2": "Windows 路径修复：使用 PathUtils 拼接令牌和启动脚本路径。",
    "0.3.3": "SSH 可执行文件显式配置及 ssh.exe／系统 OpenSSH 查找。",
    "0.3.4": "曾加入 MinerU disabled 配置的中间版本；此设计在 0.3.5 撤回，仅供历史审计。",
    "0.3.5": "显示索引准备状态；撤回 MinerU disabled 配置，恢复 local/remote。同期后端改善缓存与失败分类，详见故障记录。",
    "0.3.6": "增加 MinerU 端口转发；区分候选池与实际入选证据，展示检索与纠正次数。",
    "0.3.7": "Zotero 10 复数分类接口兼容；拒绝混合选择；个人记忆改写轨迹、模型说明及服务地址提示。",
    "0.3.8": "增加 reranker 端口转发；展示检索算法及精排成功／降级状态。",
    "0.3.9": "实验图文问答：显式图片支持配置、受控图片展示、视觉观察标签及草稿来源隔离。",
    "0.3.10": "实验生成前充分性检查开关与轨迹；明确图片重复发送计费。质量未达生产门禁要求。",
    "0.3.11": "产品收尾：后端复核与笔记范围严格布尔校验、严格 claim index；补公开实验收据 CI、演示、简历事实清单与历史版本归档。插件业务资产相对 0.3.10 不变。",
    "0.3.12": "构建修复：所有插件文本资产统一LF换行，消除Git autocrlf导致的跨平台XPI哈希差异；新增LF／CRLF回归，并从干净提交源码重建核验。",
}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def git(*args, data=None, env=None):
    return subprocess.check_output(["git", *args], cwd=ROOT, input=data, env=env)


def request(url, token=None, payload=None, method=None, content_type="application/json"):
    body = json.dumps(payload).encode() if isinstance(payload, dict) else payload
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "research-assistant-release-archive"}
    if token:
        headers["Authorization"] = "Bearer " + token
    if body is not None:
        headers["Content-Type"] = content_type
    with urllib.request.urlopen(urllib.request.Request(url, data=body, headers=headers, method=method), timeout=60) as response:
        return response.read()


def prepare(output):
    output.mkdir(parents=True, exist_ok=False)
    records = []
    previous = {}
    for version, description in NOTES.items():
        path = ROOT / ("research-assistant-0.1.0.xpi" if version == "0.1.0" else
                       f"output/zotero/research-assistant-{version}.xpi")
        raw = path.read_bytes()
        local_variant = None
        if version == "0.2.0":
            local_variant = raw
            raw = request(f"https://github.com/{REPO}/releases/download/research/research-assistant-0.2.0.xpi")
            if sha(raw) != "f09a319846fa0abe3b1668f46e7bbde714bc00781be46dabbfd94854b052866e":
                raise ValueError("Existing published 0.2.0 does not match audited digest")
        folder = output / version
        folder.mkdir()
        name = f"research-assistant-{version}.xpi"
        (folder / name).write_bytes(raw)
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            assets = {n: z.read(n) for n in z.namelist()}
        if any(not re.fullmatch(r"[A-Za-z0-9_.-]+", n) for n in assets):
            raise ValueError("Unexpected archive member")
        if json.loads(assets["manifest.json"])["version"] != version:
            raise ValueError("Manifest version mismatch")
        changed = sorted(n for n, b in assets.items() if previous.get(n) != b)
        source = folder / "source" / "deployment" / "zotero"
        source.mkdir(parents=True)
        for n, b in assets.items():
            (source / n).write_bytes(b)
        boundary = ("当前完整源码提交；本轮未重新运行真实模型／原生 Zotero 验收。"
                    if version in FULL_SOURCE_VERSIONS else
                    "2026-11-18 补录的历史归档，不是当时已发布的声明。标签源码由此 XPI 精确还原，仅包含插件；不包含或冒充该时点的完整后端快照。历史包不建议用于当前生产。")
        body = f"# v{version}\n\n{description}\n\n{boundary}\n\n插件需要独立 Python 后端、模型和用户配置；XPI 不携带这些服务。\n\n相较上一归档包变化的文件：{', '.join(changed)}。\n\nSHA-256 `{sha(raw)}`\n\n更新依据：留存包逐文件比对、项目问题排查笔记及验收文档；不追溯伪造发布日期。\n"
        (folder / "release-notes.md").write_text(body, encoding="utf-8")
        (folder / "source" / "ARCHIVE.md").write_text(body, encoding="utf-8")
        checksums = {name: sha(raw)}
        if local_variant is not None and local_variant != raw:
            alt = f"research-assistant-{version}-local-retained.xpi"
            (folder / alt).write_bytes(local_variant)
            checksums[alt] = sha(local_variant)
        (folder / "SHA256SUMS.txt").write_text("".join(f"{value}  {key}\n" for key, value in checksums.items()), encoding="utf-8")
        records.append({"version": version, "description": description, "assets": checksums,
                        "changed_plugin_files": changed, "historical_backend_available": version in FULL_SOURCE_VERSIONS})
        previous = assets
    (output / "manifest.json").write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"versions": len(records), "output": str(output)}))


def archive_tag(folder, version):
    tag = "v" + version
    existing = subprocess.run(["git", "rev-parse", "--verify", "refs/tags/" + tag], cwd=ROOT, capture_output=True)
    if existing.returncode == 0:
        commit = git("rev-parse", tag + "^{commit}").decode().strip()
        validate_tag(folder, version, commit)
        return commit
    if version in FULL_SOURCE_VERSIONS:
        commit = git("rev-parse", "HEAD").decode().strip()
        if json.loads(git("show", commit + ":deployment/zotero/manifest.json"))["version"] != version:
            raise ValueError("Commit version differs from requested full-source release")
    else:
        # Build an isolated Git index without changing the checkout or its index.
        import os
        index = folder.resolve() / "archive.index"
        if index.exists():
            raise ValueError("Unexpected existing archive index")
        env = {**os.environ, "GIT_INDEX_FILE": str(index)}
        git("read-tree", "--empty", env=env)
        for p in sorted((folder / "source").rglob("*")):
            if p.is_file():
                blob = git("hash-object", "-w", "--stdin", data=p.read_bytes()).decode().strip()
                git("update-index", "--add", "--cacheinfo", "100644", blob,
                    p.relative_to(folder / "source").as_posix(), env=env)
        tree = git("write-tree", env=env).decode().strip()
        commit = git("commit-tree", tree, "-m", f"Archive retained v{version} plugin bytes (backend snapshot unavailable)").decode().strip()
        index.unlink()
    git("tag", "-a", tag, commit, "-m", f"v{version}: archived 2026-11-18; see ARCHIVE.md/release notes for provenance")
    return commit


def validate_tag(folder, version, commit):
    source = folder / "source"
    if version in FULL_SOURCE_VERSIONS:
        paths = (source / "deployment" / "zotero").iterdir()
    else:
        paths = (p for p in source.rglob("*") if p.is_file())
        actual = set(git("ls-tree", "-r", "--name-only", commit).decode().splitlines())
        expected = {p.relative_to(source).as_posix() for p in source.rglob("*") if p.is_file()}
        if actual != expected:
            raise ValueError("Historical tag tree differs from prepared archive")
    for p in paths:
        # Main source may use platform-native line endings; historical blobs are exact.
        actual = git("show", commit + ":" + p.relative_to(source).as_posix())
        expected = p.read_bytes()
        if version in FULL_SOURCE_VERSIONS:
            actual, expected = actual.replace(b"\r\n", b"\n"), expected.replace(b"\r\n", b"\n")
        if actual != expected:
            raise ValueError("Tag source does not match prepared plugin: " + p.name)


def publish(output):
    import os
    if git("status", "--porcelain").strip():
        raise ValueError("Commit the complete public source before publishing")
    if git("ls-files", "-ci", "--exclude-from=.gitignore").strip():
        raise ValueError("Ignored local files are tracked")
    if git("rev-parse", "HEAD") != git("rev-parse", "origin/main"):
        raise ValueError("Push current source before publishing")
    credentials = subprocess.run(["git", "credential", "fill"], input="protocol=https\nhost=github.com\n\n",
                                 text=True, capture_output=True, check=True,
                                 env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"})
    token = dict(l.split("=", 1) for l in credentials.stdout.splitlines() if "=" in l).get("password")
    if not token:
        raise ValueError("No GitHub credential available")
    receipts = []
    for record in json.loads((output / "manifest.json").read_text(encoding="utf-8")):
        version = record["version"]
        if version in WITHDRAWN_XPI_VERSIONS:
            continue
        folder = output / version
        for name, expected in record["assets"].items():
            if sha((folder / name).read_bytes()) != expected:
                raise ValueError("Archive asset changed")
        commit = archive_tag(folder, version)
        validate_tag(folder, version, commit)
        git("push", "origin", "refs/tags/v" + version)
        endpoint = f"{API}/repos/{REPO}/releases"
        try:
            release = json.loads(request(endpoint + "/tags/v" + version, token))
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
            release = json.loads(request(endpoint, token, {
                "tag_name": "v" + version, "name": f"v{version} — {'收尾候选' if version in FULL_SOURCE_VERSIONS else '历史产物归档'}",
                "body": (folder / "release-notes.md").read_text(encoding="utf-8"),
                "draft": True, "prerelease": True, "make_latest": "false"}))
        existing = {a["name"]: a for a in release["assets"]}
        if release["body"] != (folder / "release-notes.md").read_text(encoding="utf-8") or not release["prerelease"]:
            raise ValueError("Existing release metadata differs from prepared archive")
        for name in [*record["assets"], "SHA256SUMS.txt"]:
            raw = (folder / name).read_bytes()
            if name in existing:
                if existing[name].get("digest") != "sha256:" + sha(raw):
                    raise ValueError("Existing release asset differs: " + name)
                continue
            request(release["upload_url"].split("{")[0] + "?name=" + urllib.parse.quote(name), token,
                    raw, content_type="application/octet-stream")
        if release["draft"]:
            release = json.loads(request(endpoint + "/" + str(release["id"]), token,
                                         {"draft": False, "prerelease": True, "make_latest": "false"}, method="PATCH"))
        receipts.append({"version": version, "url": release["html_url"], "commit": commit})
        (output / "published.json").write_text(json.dumps(receipts, indent=2), encoding="utf-8")
        print(release["html_url"], flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "publish"])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    (prepare if args.action == "prepare" else publish)(args.output)
