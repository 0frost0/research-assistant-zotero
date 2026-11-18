"""Copy an existing knowledge library for bounded native Zotero acceptance.

Private data stays under ignored output/. No synthetic documents or model doubles.
"""
import argparse
import json
from pathlib import Path
import shutil
import sqlite3


def prepare(source, target, connection, base, port, comparison=False, workflow=False):
    source, target = source.resolve(), target.resolve()
    if not target.is_relative_to(source / "output"):
        raise ValueError("Target must be a new directory under project output/")
    target.mkdir(parents=True, exist_ok=False)
    home = target / "backend/.data/zotero"
    home.mkdir(parents=True)
    original = source / ".data/zotero"
    for name in ("knowledge.sqlite3", "qa_jobs.sqlite3"):
        with sqlite3.connect((original / name).as_uri() + "?mode=ro", uri=True) as src:
            with sqlite3.connect(home / name) as dst:
                src.backup(dst)
    for name in ("service-config.json", "connection-token"):
        shutil.copy2(original / name, home / name)
    shutil.copytree(original / "objects", home / "objects")
    cache = original / "indexes/embedding-cache.sqlite3"
    if cache.exists():
        (home / "indexes").mkdir()
        with sqlite3.connect(cache.as_uri() + "?mode=ro", uri=True) as src:
            with sqlite3.connect(home / "indexes/embedding-cache.sqlite3") as dst:
                src.backup(dst)
    profile, data, plugin = target / "profile", target / "data", target / "plugin"
    profile.mkdir()
    data.mkdir()
    shutil.copytree(source / "deployment/zotero", plugin)
    prefs = {"extensions.zotero.dataDir": str(data), "extensions.zotero.useDataDir": True,
             "extensions.zotero.sync.autoSync": False, "extensions.update.enabled": False,
             "extensions.zoteroWinWordIntegration.skipInstallation": True,
             "extensions.zoteroOpenOfficeIntegration.skipInstallation": True,
             "devtools.debugger.remote-enabled": True, "devtools.debugger.prompt-connection": False,
             "devtools.debugger.force-local": True, "app.update.auto": False}
    (profile / "user.js").write_text("\n".join(f"user_pref({json.dumps(k)}, {json.dumps(v)});" for k, v in prefs.items()), encoding="utf-8")
    cases = [
        {"id": "definition", "query": "什么是EHR？", "scope": "original"},
        {"id": "method", "query": "RePrompT使用了哪些数据集？", "scope": "original"},
        {"id": "parameter", "query": "MOTOR模型有多少参数？", "scope": "original"},
        {"id": "memory", "query": "我记录的EHR事件时间间隔有什么特点？", "scope": "notes"},
        {"id": "absent", "query": "这些文献中火星基地的建造预算是多少？", "scope": "original"},
    ]
    config = {"root": str(target), "connection": connection, "base": base,
              "endpoint": f"http://127.0.0.1:{port}", "cases": cases, "max_model_calls": 18}
    script = "zotero_real_workflow.js" if workflow else "zotero_real_comparison.js" if comparison else "zotero_real_questions.js"
    if workflow:
        # Copy one real PDF to import through Zotero. Remove only its isolated
        # object bytes so the bridge must exercise PUT rather than a cache hit.
        with sqlite3.connect((home / "knowledge.sqlite3").as_uri() + "?mode=ro", uri=True) as db:
            candidates = db.execute("SELECT hash FROM objects").fetchall()
        pdf = min((home / "objects" / (sha + ".pdf") for (sha,) in candidates), key=lambda p: p.stat().st_size)
        shutil.copy2(pdf, target / "workflow-paper.pdf")
        config["workflow_sha"] = pdf.stem
        pdf.unlink()
        # This run explicitly submits parsing through the UI, not on sync.
        config_path = home / "service-config.json"
        services = json.loads(config_path.read_text(encoding="utf-8"))
        services[connection]["mineru"]["auto_parse"] = False
        config_path.write_text(json.dumps(services, ensure_ascii=False), encoding="utf-8")
    (target / "run-config.json").write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    js = (source / "benchmarks" / script).read_text(encoding="utf-8")
    (plugin / "real-run.js").write_text(js.replace("__RUN_CONFIG__", json.dumps(config, ensure_ascii=False)), encoding="utf-8")
    bootstrap = (plugin / "bootstrap.js").read_text(encoding="utf-8")
    bootstrap = bootstrap.replace("for (const window of Zotero.getMainWindows()) bridgeScope.ResearchBridge.loadWindow(window);",
        "for (const window of Zotero.getMainWindows()) bridgeScope.ResearchBridge.loadWindow(window);\n"
        "  Services.scriptloader.loadSubScript(rootURI + 'real-run.js', bridgeScope);\n"
        "  void bridgeScope.runRealQuestions();")
    (plugin / "bootstrap.js").write_text(bootstrap, encoding="utf-8")
    print(target)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    p.add_argument("--target", type=Path, required=True)
    p.add_argument("--connection", required=True)
    p.add_argument("--base", required=True)
    p.add_argument("--port", type=int, default=8776)
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--comparison", action="store_true", help="Run bounded native two-paper comparison instead of five QA cases")
    mode.add_argument("--workflow", action="store_true", help="Run native sync, upload, note history and parsing on an isolated real PDF")
    a = p.parse_args()
    prepare(a.source, a.target, a.connection, a.base, a.port, a.comparison, a.workflow)
