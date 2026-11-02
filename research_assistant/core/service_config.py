from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from threading import RLock


class ServiceConfigStore:
    """
    保存每个 Zotero connection 的服务配置。

    API Key 只保存在后端，不返回给 Zotero 页面。
    """

    def __init__(self, root: Path):
        self.path = root / ".data" / "zotero" / "service-config.json"
        self.lock = RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _read(self) -> dict:
        if not self.path.is_file():
            return {}

        try:
            value = json.loads(
                self.path.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            raise ValueError("服务配置文件无法读取；为保护已有配置，未覆盖文件。") from None

        if not isinstance(value, dict):
            raise ValueError("服务配置文件格式无效，未覆盖文件。")
        return value

    def _write(self, value: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)

        fd, temporary = tempfile.mkstemp(
            prefix="service-config-",
            suffix=".tmp",
            dir=self.path.parent,
        )

        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(
                    value,
                    stream,
                    ensure_ascii=False,
                    indent=2,
                )
                stream.flush()
                os.fsync(stream.fileno())

            os.replace(temporary, self.path)

            # Linux / macOS 下限制为当前用户可读写。
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass

        finally:
            Path(temporary).unlink(missing_ok=True)

    def get(self, connection: str) -> dict:
        with self.lock:
            return self._read().get(connection, {})

    def save(self, connection: str, value: dict) -> dict:
        with self.lock:
            all_configs = self._read()
            old = all_configs.get(connection, {})
            for section in ("llm", "embedding"):
                # Empty password fields mean keep, never erase an existing key.
                previous = old.get(section, {})
                current = value[section]
                if not current["api_key"]:
                    if previous.get("base_url") != current["base_url"]:
                        raise ValueError("更换 API 地址时必须重新输入密钥。")
                    current["api_key"] = previous.get("api_key", "")
                if not current["api_key"] or len(current["api_key"]) > 8192:
                    raise ValueError("请填写有效 API Key。")
            all_configs[connection] = value
            self._write(all_configs)
            return value
