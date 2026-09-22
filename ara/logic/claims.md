# Claims

## C01: Zotero 9 requires an update URL in legacy extension manifests
- **Statement**: On the installed Zotero 9.0.6 build, a bootstrapped manifest without `applications.zotero.update_url` is rejected as invalid.
- **Status**: supported
- **Provenance**: ai-suggested
- **Falsification criteria**: The same manifest installs on the same build without the field, or Zotero reports a different validation cause.
- **Proof**: [E01]
- **Dependencies**: []
- **Tags**: zotero, plugin, compatibility

## C02: The packaged bridge passes isolated native acceptance
- **Statement**: `research-assistant-0.1.0.xpi` can be activated by Zotero 9.0.6 in an isolated profile and creates its Tools menu.
- **Status**: supported
- **Provenance**: ai-suggested
- **Falsification criteria**: Repeating the isolated package installation fails activation or menu creation.
- **Proof**: [E01]
- **Dependencies**: [C01]
- **Tags**: zotero, acceptance, packaging

## C03: The integrated Zotero workbench works from the packaged 0.2.0 extension
- **Statement**: `research-assistant-0.2.0.xpi` activates in Zotero 9.0.6, and the packaged internal workbench can render its controls, connect to the Python backend, synchronize a collection, and write a note revision idempotently in an isolated profile.
- **Status**: supported
- **Provenance**: ai-suggested
- **Falsification criteria**: Repeating the isolated final-XPI installation or packaged fixture acceptance fails activation, UI rendering, synchronization, backend status, or idempotent writeback.
- **Proof**: [E02]
- **Dependencies**: [C01, C02]
- **Tags**: zotero, python, workbench, packaging, acceptance
