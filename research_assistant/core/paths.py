"""项目路径的唯一入口：移动业务模块不应改变数据位置。"""

from pathlib import Path
import json
from functools import lru_cache
import json
from functools import lru_cache

# paths.py -> core -> research_assistant 包 -> 项目根目录。
# 不依赖终端当前目录；library、.data 和 .cache 继续使用原位置。
PROJECT_ROOT = Path(__file__).resolve().parents[2]
STATIC_DIR = PROJECT_ROOT / "research_assistant" / "web" / "static"


@lru_cache(maxsize=1)
def _version_path_identity():
    path = PROJECT_ROOT / '.data/path_identity.json'
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}


def version_metadata(metadata):
    """A host migration changes file locations, not the identity of evidence.

    Only the hash input of migrated sources uses its original path namespace.
    Stored/displayed metadata keeps the actual current file locations.
    """
    identity = _version_path_identity()
    if metadata.get('source') not in identity.get('sources', []):
        return metadata
    image = metadata.get('image_path')
    prefix = str(PROJECT_ROOT).replace('\\', '/') + '/'
    if isinstance(image, str) and image.replace('\\', '/').startswith(prefix):
        relative = image.replace('\\', '/')[len(prefix):]
        return dict(metadata, image_path=identity['original_root'].rstrip('\\/') + '\\' + relative.replace('/', '\\'))
    return metadata


@lru_cache(maxsize=1)
def _version_path_identity():
    path = PROJECT_ROOT / '.data/path_identity.json'
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}


def version_metadata(metadata):
    """A host migration changes file locations, not the identity of evidence.

    Only the hash input of migrated sources uses its original path namespace.
    Stored/displayed metadata keeps the actual current file locations.
    """
    identity = _version_path_identity()
    if metadata.get('source') not in identity.get('sources', []):
        return metadata
    image = metadata.get('image_path')
    prefix = str(PROJECT_ROOT).replace('\\', '/') + '/'
    if isinstance(image, str) and image.replace('\\', '/').startswith(prefix):
        relative = image.replace('\\', '/')[len(prefix):]
        return dict(metadata, image_path=identity['original_root'].rstrip('\\/') + '\\' + relative.replace('/', '\\'))
    return metadata
