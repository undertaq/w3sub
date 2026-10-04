"""Validate and run the separate WolvenKit v164 reference helper.

The batch protocol preserves every typed LocalizedString owner, field and ID.
The index builder establishes inventory coverage and classifies these uses.
The older scene-export functions remain available for prototype callers.
"""
from collections.abc import Callable, Mapping
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import subprocess


class SceneReferenceError(ValueError):
    """A WolvenKit export does not match the expected CR2W JSON structure."""


@dataclass(frozen=True)
class SceneDialogueReference:
    string_id: int
    field: str
    source_scene: str


_DIALOGUE_FIELDS = {
    "CStorySceneLine": "dialogLine",
    "CStorySceneChoiceLine": "choiceLine",
}
_DECIMAL_ID = re.compile(r"[0-9]+\Z")


def extract_scene_dialogue_references(
        document: object, *, source_scene: str = "") -> tuple[SceneDialogueReference, ...]:
    """Return IDs only from CStorySceneLine/ChoiceLine LocalizedString fields.

    Other LocalizedString fields in a scene are deliberately ignored. If an
    expected subtitle field exists but WolvenKit exports it with an unexpected
    shape or value, fail instead of silently building an incomplete index.
    """
    if not isinstance(document, Mapping) or document.get("_type") != "CR2W":
        raise SceneReferenceError("Expected a WolvenKit CR2W JSON document")
    chunks = document.get("_chunks")
    if not isinstance(chunks, Mapping):
        raise SceneReferenceError("CR2W JSON is missing its _chunks object")

    references: set[SceneDialogueReference] = set()
    for chunk_key, chunk in chunks.items():
        if not isinstance(chunk, Mapping):
            raise SceneReferenceError(f"Invalid CR2W chunk {chunk_key!r}")
        field_name = _DIALOGUE_FIELDS.get(chunk.get("_type"))
        if field_name is None:
            continue
        variables = chunk.get("_vars")
        if not isinstance(variables, Mapping):
            raise SceneReferenceError(f"Scene line {chunk_key!r} is missing _vars")
        localized = variables.get(field_name)
        if localized is None:
            # Some authored line chunks intentionally contain no localized text.
            continue
        if not isinstance(localized, Mapping) or localized.get("_type") != "LocalizedString":
            raise SceneReferenceError(
                f"{chunk_key!r}.{field_name} is not a typed LocalizedString"
            )
        raw_id = localized.get("_value")
        if isinstance(raw_id, bool):
            raise SceneReferenceError(f"Invalid string ID in {chunk_key!r}.{field_name}")
        if isinstance(raw_id, int):
            string_id = raw_id
        elif isinstance(raw_id, str) and _DECIMAL_ID.fullmatch(raw_id):
            string_id = int(raw_id)
        else:
            raise SceneReferenceError(f"Invalid string ID in {chunk_key!r}.{field_name}")
        if not 0 <= string_id <= 0xFFFFFFFF:
            raise SceneReferenceError(f"String ID is outside uint32 in {chunk_key!r}.{field_name}")
        references.add(SceneDialogueReference(string_id, field_name, source_scene))
    return tuple(sorted(references, key=lambda item: (item.string_id, item.field, item.source_scene)))


def load_scene_dialogue_references(
        path: Path, *, source_scene: str | None = None) -> tuple[SceneDialogueReference, ...]:
    """Load a WolvenKit CR2W JSON export from disk."""
    path = Path(path)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise SceneReferenceError(f"Cannot read WolvenKit JSON {path}: {error}") from error
    return extract_scene_dialogue_references(
        document,
        source_scene=source_scene if source_scene is not None else path.name,
    )

# Separate-process WolvenKit batch protocol. Keep the scene-export API above for
# existing callers and prototype diagnostics.
TRUSTED_MANIFEST_SHA256 = 'd2c316e2da9381aaf3fef45f3ae881440d1d69c91106e3114f995011355053dd'
HELPER_VERSION = 'w3sub-wolvenkit7-v164-1'
UPSTREAM_COMMIT = '8eb4026349b1e419c189d42b8286b4a5613ab433'
_HELPER_ROOT = Path(__file__).resolve().parent.parent / 'tools' / 'wolvenkit7-v164'
DEFAULT_HELPER_PATH = _HELPER_ROOT / 'bin' / 'WolvenKit.CLI.exe'


@dataclass(frozen=True)
class LocalizedReference:
    string_id: int
    resource_identity: str
    owner_type: str
    field_name: str


@dataclass(frozen=True)
class HelperIdentity:
    version: str
    upstream_commit: str
    executable_sha256: str
    supports_v164: bool


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise SceneReferenceError(f'Cannot read helper metadata {path}: {error}') from error


def _run_helper(arguments: list[str], *, timeout: int, cwd: Path | None = None):
    try:
        return subprocess.run(arguments, cwd=cwd, capture_output=True, text=True,
                              encoding='utf-8-sig', errors='strict', timeout=timeout,
                              creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except (OSError, UnicodeError, subprocess.TimeoutExpired) as error:
        raise SceneReferenceError(f'WolvenKit helper unavailable: {error}') from error


def validate_wolvenkit_helper(helper_path: Path) -> HelperIdentity:
    """Check the pinned package (including DLLs/source), then its batch protocol."""
    helper_path = Path(helper_path).resolve()
    metadata_path = helper_path.parent.parent / 'helper-manifest.json'
    try:
        if _sha256(metadata_path) != TRUSTED_MANIFEST_SHA256:
            raise SceneReferenceError('Helper manifest is modified or unrecognized')
    except OSError as error:
        raise SceneReferenceError(f'Missing helper manifest: {error}') from error
    metadata = _read_json(metadata_path)
    if (not isinstance(metadata, dict) or type(metadata.get('schema')) is not int or metadata.get('schema') != 1
            or metadata.get('version') != HELPER_VERSION
            or metadata.get('upstream_commit') != UPSTREAM_COMMIT
            or metadata.get('supports_v164') is not True):
        raise SceneReferenceError('Unrecognized WolvenKit helper build')
    files = metadata.get('files')
    if not isinstance(files, dict) or f'bin/{helper_path.name}' not in files:
        raise SceneReferenceError('Helper package has no executable identity')
    package_root = metadata_path.parent.resolve()
    for name, expected in files.items():
        if not isinstance(name, str) or not isinstance(expected, str) or not re.fullmatch('[0-9a-f]{64}', expected):
            raise SceneReferenceError('Malformed helper package file identity')
        path = (package_root / name).resolve()
        if not path.is_relative_to(package_root):
            raise SceneReferenceError('Helper package path escapes its directory')
        try:
            actual = _sha256(path)
        except OSError as error:
            raise SceneReferenceError(f'Missing helper package file {name}: {error}') from error
        if actual != expected:
            raise SceneReferenceError(f'Modified helper package file: {name}')
    # Assemblies next to an executable participate in loading. Unknown runtime
    # files must not silently replace an assembly or its binding configuration.
    runtime_files = {p.relative_to(package_root).as_posix() for p in helper_path.parent.rglob('*') if p.is_file()}
    expected_runtime = {name for name in files if name.startswith('bin/')}
    source_packages = metadata.get('source_packages')
    if (runtime_files != expected_runtime or not isinstance(source_packages, list)
            or not source_packages or any(name not in files for name in source_packages)):
        raise SceneReferenceError('Incomplete or modified helper distribution')
    result = _run_helper([str(helper_path), '--w3sub-identity'], timeout=30)
    try:
        identity = json.loads(result.stdout)
    except (ValueError, TypeError) as error:
        raise SceneReferenceError(f'Invalid helper identity: {result.stderr}') from error
    if result.returncode or identity != {
        'schema': 1, 'version': HELPER_VERSION, 'upstream_commit': UPSTREAM_COMMIT,
        'supports_v164': True,
    }:
        raise SceneReferenceError(f'Incompatible WolvenKit helper: {result.stderr}')
    return HelperIdentity(HELPER_VERSION, UPSTREAM_COMMIT,
                          files[f'bin/{helper_path.name}'], True)


def scan_localized_references(
        manifest_path: Path, helper_path: Path, work_dir: Path,
        progress_callback: Callable[[int, int], None] | None = None,
) -> tuple[LocalizedReference, ...]:
    """Inspect a complete batch; never return successful results from a partial scan.

    Manifest: {schema: 1, resources: [{path: absolute path,
    resource_identity: stable physical entry identity}]}. Output records use
    schema 1, resource_identity, ok, references (string_id/owner_type/field_name).
    No game text is written by this adapter.
    """
    manifest_path, helper_path, work_dir = map(Path, (manifest_path, helper_path, work_dir))
    validate_wolvenkit_helper(helper_path)
    manifest = _read_json(manifest_path)
    if not isinstance(manifest, dict) or type(manifest.get('schema')) is not int or manifest.get('schema') != 1 or not isinstance(manifest.get('resources'), list):
        raise SceneReferenceError('Invalid resource manifest')
    resources = manifest['resources']
    expected = set()
    for resource in resources:
        if (not isinstance(resource, dict) or set(resource) != {'path', 'resource_identity'}
                or not isinstance(resource['path'], str) or not Path(resource['path']).is_absolute()
                or not isinstance(resource['resource_identity'], str) or not resource['resource_identity']
                or resource['resource_identity'] in expected):
            raise SceneReferenceError('Malformed or duplicate manifest resource')
        expected.add(resource['resource_identity'])
    work_dir.mkdir(parents=True, exist_ok=True)
    if progress_callback:
        progress_callback(0, len(expected))
    result = _run_helper([str(helper_path.resolve()), '--w3sub-batch', str(manifest_path.resolve())],
                         timeout=max(120, len(expected) * 10), cwd=work_dir)
    seen = set()
    references = set()
    for line in result.stdout.splitlines():
        try:
            record = json.loads(line)
        except (ValueError, TypeError) as error:
            raise SceneReferenceError(f'Malformed helper result: {line[:200]}') from error
        if (not isinstance(record, dict) or type(record.get('schema')) is not int or record.get('schema') != 1
                or not isinstance(record.get('resource_identity'), str)
                or record['resource_identity'] not in expected or record['resource_identity'] in seen
                or type(record.get('ok')) is not bool):
            raise SceneReferenceError('Unrecognized or duplicate helper result')
        identity = record['resource_identity']
        if not record['ok']:
            raise SceneReferenceError(f'{identity}: {record.get("error", "parse failed")}')
        if set(record) != {'schema', 'resource_identity', 'ok', 'references'} or not isinstance(record['references'], list):
            raise SceneReferenceError(f'Malformed references for {identity}')
        for ref in record['references']:
            if (not isinstance(ref, dict) or set(ref) != {'string_id', 'owner_type', 'field_name'}
                    or type(ref['string_id']) is not int or not 0 <= ref['string_id'] <= 0xFFFFFFFF
                    or not isinstance(ref['owner_type'], str) or not ref['owner_type'].strip()
                    or not isinstance(ref['field_name'], str) or not ref['field_name'].strip()):
                raise SceneReferenceError(f'Malformed typed LocalizedString reference in {identity}')
            references.add(LocalizedReference(ref['string_id'], identity, ref['owner_type'], ref['field_name']))
        seen.add(identity)
        if progress_callback:
            progress_callback(len(seen), len(expected))
    if result.returncode:
        raise SceneReferenceError(f'WolvenKit batch failed ({result.returncode}): {result.stderr[:2000]}')
    if seen != expected:
        missing = ', '.join(sorted(expected - seen)[:5])
        raise SceneReferenceError(f'Incomplete WolvenKit batch: missing {missing}')
    return tuple(sorted(references, key=lambda r: (r.resource_identity, r.string_id, r.owner_type, r.field_name)))
