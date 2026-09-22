# Heuristics

## H01: Keep native add-on fixtures outside the Zotero profile
- **Rationale**: Zotero startup may remove an unregistered XPI placed directly in `profile/extensions`; load the fixture directory with `installTemporaryAddon()` instead.
- **Provenance**: ai-suggested
- **Sensitivity**: medium
- **Code ref**: [`benchmarks/install_zotero_fixture.py`, `benchmarks/pack_zotero_fixture.py`]

## H02: Use platform path utilities inside Zotero
- **Rationale**: `Zotero.File` and `nsIFile` rejected slash-concatenated Windows paths during native acceptance; `PathUtils.join()` preserves the expected platform form.
- **Provenance**: ai-suggested
- **Sensitivity**: high
- **Code ref**: [`benchmarks/zotero_native_fixture.js`, `benchmarks/install_zotero_fixture.py`]

## H03: Keep the public plugin repository independently buildable
- **Rationale**: A root-level `build.py` lets contributors build the XPI without cloning the complete research assistant backend or relying on its directory layout.
- **Provenance**: ai-suggested
- **Sensitivity**: low
- **Code ref**: [`deployment/zotero/build.py`, `deployment/zotero/README.md`]

## H04: Register chrome content explicitly in bootstrapped Zotero plugins
- **Rationale**: A packaged add-on has a jar root URI, while an internal dialog needs a stable chrome content URL. Registering the content package with `amIAddonManagerStartup.registerChrome()` works for both source and XPI installation and provides a destructible shutdown handle.
- **Provenance**: ai-suggested
- **Sensitivity**: high
- **Code ref**: [`deployment/zotero/bootstrap.js`, `deployment/zotero/bridge.js`]

## H05: Initialize XHTML workbench scripts after the body DOM
- **Rationale**: Zotero's XHTML/chrome loading did not reliably honor a head-level `defer` script before control lookup. Placing the script at the end of `body` ensures required controls exist before listeners are attached.
- **Provenance**: ai-suggested
- **Sensitivity**: high
- **Code ref**: [`deployment/zotero/assistant.xhtml`, `deployment/zotero/assistant.js`]
