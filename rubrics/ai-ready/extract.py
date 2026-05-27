"""
RO-Crate AI-Ready evidence extractor.

Loads a top-level RO-Crate plus any sub-crates referenced via
'ro-crate-metadata' pointers in the main @graph, walks the merged
graph, and produces the structured `extractor_inputs` each rubric
needs (counts, samples, narrative fields).

Use as a library:

    from extract import load_release, extract_all_inputs
    bundle = load_release("/path/to/ro-crate-metadata.json")
    inputs = extract_all_inputs(bundle)

Or run as a script to dump the inputs as JSON to stdout / file.

Structure:

    ReleaseBundle              -- load + dedup root crate and sub-crates
    StandardsDetector          -- recognize standard vocab in @context
    ArchiveDetector            -- recognize FAIR-compliant archives
    AccessionDetector          -- recognize specialist-repo accessions
    OntologyDetector           -- count / sample ontology IRIs
    SchemaStandardDetector     -- detect JSON Schema / Frictionless conformance
    RubricExtractor            -- abstract base for the 28 per-rubric classes
    Findable, Accessible, ...  -- 28 concrete extractors

Public API (load_release / extract_all_inputs / CLI) preserved.
"""
from __future__ import annotations

import json
import re
import sys
from abc import ABC, abstractmethod
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, ClassVar, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def _read_json(path: Path) -> Optional[dict]:
    try:
        with path.open("r") as f:
            return json.load(f)
    except Exception as exc:
        sys.stderr.write(f"[warn] failed to read {path}: {exc}\n")
        return None


class ReleaseBundle:
    """Root RO-Crate + sub-crates, merged & deduplicated by @id.

    Holds the merged @graph as raw dicts so the extractor can use the
    full set of field aliases (RO-Crate, EVI, PROV, RAI, D4D) without
    fighting pydantic validation on a real-world crate. The
    ``fairscape_models`` package is consulted as the *reference* for
    which aliases exist, not as a strict validator.
    """

    def __init__(
        self,
        main_path: Path,
        main_crate: dict,
        sub_crates: List[dict],
        entities: List[dict],
        root_entity: dict,
        sub_root_entities: List[dict],
        source_by_id: Optional[Dict[str, str]] = None,
    ):
        self.main_path = main_path
        self.crate_dir = main_path.parent
        self.main_crate = main_crate
        self.sub_crates = sub_crates
        self.entities = entities
        self.root_entity = root_entity
        self.sub_root_entities = sub_root_entities
        self.source_by_id: Dict[str, str] = source_by_id or {}

        # @context normalized to a flat list of strings
        context = main_crate.get("@context")
        ctx_ns: List[str] = []
        if isinstance(context, dict):
            ctx_ns = [str(v) for v in context.values()]
        elif isinstance(context, list):
            for c in context:
                if isinstance(c, str):
                    ctx_ns.append(c)
                elif isinstance(c, dict):
                    ctx_ns.extend(str(v) for v in c.values())
        elif isinstance(context, str):
            ctx_ns = [context]
        self.context_namespaces = ctx_ns

    # ----- typed accessors (return raw dicts filtered by @type) -----

    def datasets(self) -> List[dict]:
        return [e for e in self.entities if is_dataset(e)]

    def software(self) -> List[dict]:
        return [e for e in self.entities if is_software(e)]

    def computations(self) -> List[dict]:
        return [e for e in self.entities if is_computation(e)]

    def experiments(self) -> List[dict]:
        return [e for e in self.entities if is_experiment(e)]

    def schemas(self) -> List[dict]:
        return [e for e in self.entities if is_schema(e)]

    def samples(self) -> List[dict]:
        return [e for e in self.entities if is_sample(e)]

    def instruments(self) -> List[dict]:
        return [e for e in self.entities if is_instrument(e)]

    def rocrates(self) -> List[dict]:
        return [e for e in self.entities if is_rocrate(e)]

    def root(self) -> dict:
        return self.root_entity

    # ----- loader -----

    @classmethod
    def load(cls, main_metadata_path: str | Path) -> "ReleaseBundle":
        main_path = Path(main_metadata_path).resolve()
        main_dir = main_path.parent
        main_crate = _read_json(main_path)
        if main_crate is None:
            raise FileNotFoundError(main_path)

        main_graph: List[dict] = main_crate.get("@graph", [])

        # Identify the root via the metadata-file descriptor's "about" field
        descriptor = next(
            (e for e in main_graph if e.get("@id") == "ro-crate-metadata.json"),
            None,
        )
        root_id = None
        if descriptor:
            about = descriptor.get("about")
            root_id = about.get("@id") if isinstance(about, dict) else about
        root_entity = next((e for e in main_graph if e.get("@id") == root_id), None)
        if root_entity is None:
            # Fallback: first @graph entry that isn't the descriptor itself
            root_entity = next(
                (e for e in main_graph if e.get("@id") != "ro-crate-metadata.json"),
                main_graph[0] if main_graph else {},
            )

        # Walk for sub-crate pointers (entries carrying a 'ro-crate-metadata'
        # key, with a relative file path as the value)
        sub_crates: List[dict] = []
        sub_root_entities: List[dict] = []
        for entry in main_graph:
            sub_pointer = entry.get("ro-crate-metadata")
            if not sub_pointer:
                continue
            sub_path = (main_dir / sub_pointer).resolve()
            sub_doc = _read_json(sub_path)
            if not sub_doc:
                continue
            sub_graph = sub_doc.get("@graph", [])
            sub_descriptor = next(
                (e for e in sub_graph if e.get("@id") == "ro-crate-metadata.json"),
                None,
            )
            sub_root_id = None
            if sub_descriptor:
                about = sub_descriptor.get("about")
                sub_root_id = about.get("@id") if isinstance(about, dict) else about
            sub_root = next((e for e in sub_graph if e.get("@id") == sub_root_id), None)
            sub_crates.append({"path": str(sub_path), "crate": sub_doc, "ref_id": entry.get("@id")})
            if sub_root:
                sub_root_entities.append(sub_root)

        # Merge & dedupe by @id; on collision, keep the more detailed (longer) entity.
        # Track which crate each entity came from for stratified sampling later.
        seen_ids: set = set()
        merged: List[dict] = []
        source_by_id: Dict[str, str] = {}
        for e in main_graph:
            merged.append(e)
            if e.get("@id"):
                seen_ids.add(e["@id"])
                source_by_id[e["@id"]] = "main"
        for sub in sub_crates:
            sub_label = sub.get("ref_id") or Path(sub["path"]).parent.name or "sub"
            for e in sub["crate"].get("@graph", []):
                eid = e.get("@id")
                if eid and eid in seen_ids:
                    idx = next((i for i, x in enumerate(merged) if x.get("@id") == eid), None)
                    if idx is not None and len(e) > len(merged[idx]):
                        merged[idx] = e
                        source_by_id[eid] = sub_label
                    continue
                merged.append(e)
                if eid:
                    seen_ids.add(eid)
                    source_by_id[eid] = sub_label

        return cls(
            main_path=main_path,
            main_crate=main_crate,
            sub_crates=sub_crates,
            entities=merged,
            root_entity=root_entity or {},
            sub_root_entities=sub_root_entities,
            source_by_id=source_by_id,
        )


# ---------------------------------------------------------------------------
# Type and field helpers
# ---------------------------------------------------------------------------

EVI_PREFIX = "https://w3id.org/EVI#"


def type_tokens(entity: dict) -> List[str]:
    """Flat list of type strings for an entity (across @type / metadataType /
    additionalType)."""
    tokens: List[str] = []
    for key in ("@type", "metadataType", "additionalType"):
        v = entity.get(key)
        if isinstance(v, list):
            tokens.extend(str(x) for x in v)
        elif v:
            tokens.append(str(v))
    return tokens


def has_type(entity: dict, name: str) -> bool:
    return any(name in t for t in type_tokens(entity))


def is_dataset(entity: dict) -> bool:
    # ROCrates are also typed as Dataset; exclude them from "Dataset" counts.
    return has_type(entity, "Dataset") and not has_type(entity, "ROCrate")


def is_software(entity: dict) -> bool:
    return has_type(entity, "Software")


def is_schema(entity: dict) -> bool:
    return has_type(entity, "Schema")


def is_computation(entity: dict) -> bool:
    return has_type(entity, "Computation")


def is_experiment(entity: dict) -> bool:
    return has_type(entity, "Experiment")


def is_sample(entity: dict) -> bool:
    return has_type(entity, "Sample")


def is_instrument(entity: dict) -> bool:
    return has_type(entity, "Instrument")


def is_rocrate(entity: dict) -> bool:
    return has_type(entity, "ROCrate")


def first_present(entity: dict, *keys: str) -> Any:
    """Return the first non-empty value among the given keys, or None."""
    for k in keys:
        v = entity.get(k)
        if v not in (None, "", [], {}):
            return v
    return None


# Field-alias bundles drawn from fairscape_models. Each tuple lists the
# canonical key first, then known synonyms / namespaced variants.

HASH_KEYS = ("md5", "MD5", "sha256", "SHA256", "hash", "contentChecksum")
USED_SOFTWARE_KEYS = ("usedSoftware", "evi:usedSoftware", f"{EVI_PREFIX}usedSoftware")
USED_DATASET_KEYS = ("usedDataset", "evi:usedDataset", f"{EVI_PREFIX}usedDataset")
USED_SAMPLE_KEYS = ("usedSample", "evi:usedSample", f"{EVI_PREFIX}usedSample")
USED_INSTRUMENT_KEYS = ("usedInstrument", "evi:usedInstrument", f"{EVI_PREFIX}usedInstrument")
USED_ML_MODEL_KEYS = ("usedMLModel", "evi:usedMLModel")
INPUTS_KEYS = USED_DATASET_KEYS + (f"{EVI_PREFIX}inputs", "evi:inputs", "used", "prov:used")
OUTPUTS_KEYS = ("generated", "prov:generated", f"{EVI_PREFIX}outputs", "evi:outputs")
FORMAT_KEYS = ("format", "fileFormat", "encodingFormat")
SCHEMA_LINK_FALLBACK_KEYS = ("conformsTo", "dataSchema")
CONTENT_URL_KEYS = ("contentUrl", "url", "distribution")

PROV_LINK_FIELDS = (
    "wasGeneratedBy",
    "prov:wasGeneratedBy",
    "wasDerivedFrom",
    "prov:wasDerivedFrom",
    "derivedFrom",
    "generatedBy",
    "isPartOf",
    "usedByComputation",
    "evi:usedSoftware",
    f"{EVI_PREFIX}usedSoftware",
    "evi:usedSample",
    f"{EVI_PREFIX}usedSample",
    "evi:usedInstrument",
    f"{EVI_PREFIX}usedInstrument",
    "usedSoftware",
    "usedSample",
    "usedInstrument",
    "usedDataset",
    "used",
    "prov:used",
    "generated",
    "prov:generated",
)


def has_hash(entity: dict) -> bool:
    return any(first_present(entity, k) is not None for k in HASH_KEYS)


def get_used_software(entity: dict) -> Any:
    return first_present(entity, *USED_SOFTWARE_KEYS)


def get_inputs(entity: dict) -> Any:
    return first_present(entity, *INPUTS_KEYS)


def get_outputs(entity: dict) -> Any:
    return first_present(entity, *OUTPUTS_KEYS)


def get_format(entity: dict) -> Any:
    return first_present(entity, *FORMAT_KEYS)


def _local_name(key: str) -> str:
    for sep in ("#", ":"):
        if sep in key:
            key = key.rsplit(sep, 1)[-1]
    return key


def get_dataset_schema_link(entity: dict) -> Any:
    # Match any key whose local name is "schema" case-insensitively — covers
    # every prefix/case combo of evi:schema (evi:schema, evi:Schema, EVI:Schema,
    # https://w3id.org/EVI#Schema, …) plus the bare `schema` property.
    for k, v in entity.items():
        if v in (None, "", [], {}):
            continue
        if _local_name(k).lower() == "schema":
            return v
    return first_present(entity, *SCHEMA_LINK_FALLBACK_KEYS)


def has_provenance_link(entity: dict) -> bool:
    return any(first_present(entity, k) is not None for k in PROV_LINK_FIELDS)


def is_embargoed(entity: dict) -> bool:
    """Return True if the entity's content is declared embargoed."""
    url = first_present(entity, *CONTENT_URL_KEYS)
    if isinstance(url, str) and "embargo" in url.lower():
        return True
    am = entity.get("accessMode")
    if isinstance(am, str) and "embargo" in am.lower():
        return True
    return False


def as_list(v: Any) -> list:
    if v is None:
        return []
    if isinstance(v, list):
        return v
    return [v]


def get_additional_property(entity: dict, name: str) -> Optional[str]:
    for prop in (entity.get("additionalProperty") or []):
        if isinstance(prop, dict) and (prop.get("name") or "").lower() == name.lower():
            return prop.get("value")
    return None


# ---------------------------------------------------------------------------
# Detectors — small reusable helpers
# ---------------------------------------------------------------------------


class StandardsDetector:
    """Map @context namespaces to recognized standards / vocabularies."""

    MARKERS: ClassVar[Tuple[Tuple[str, str], ...]] = (
        ("schema.org", "schema.org"),
        ("w3id.org/evi", "EVI"),
        ("evi", "EVI"),
        ("dcat", "DCAT"),
        ("croissant", "Croissant"),
        ("ml-commons.org", "Croissant"),
        ("frictionlessdata", "Frictionless"),
        ("json-schema.org", "JSON Schema"),
        ("rai", "RAI"),
        ("d4d", "D4D"),
        ("prov", "PROV-O"),
        ("datacite", "DataCite"),
    )

    @classmethod
    def detect(cls, context_namespaces: List[str]) -> List[str]:
        blob = " ".join(str(x) for x in context_namespaces).lower()
        found: List[str] = []
        for marker, label in cls.MARKERS:
            if marker in blob and label not in found:
                found.append(label)
        return found


class ArchiveDetector:
    """Recognize FAIR-compliant archive hostnames in publisher / identifier
    / description text."""

    HOSTNAMES: ClassVar[Dict[str, str]] = {
        "dataverse": "Dataverse",
        "zenodo.org": "Zenodo",
        "physionet.org": "PhysioNet",
        "fairhub": "FAIRhub",
        "biostudies": "BioStudies",
        "dbgap": "dbGaP",
        "ncbi.nlm.nih.gov": "NCBI",
        "ebi.ac.uk": "EBI",
        "ncbi.nlm.nih.gov/geo": "GEO",
        "massive.ucsd.edu": "MassIVE",
        "proteinatlas": "Human Protein Atlas",
        "softwareheritage.org": "Software Heritage",
        "figshare.com": "Figshare",
        "osf.io": "OSF",
        "dryad": "Dryad",
        "github.com": "GitHub",
    }

    @classmethod
    def detect(cls, *texts: Optional[Any]) -> List[str]:
        found: List[str] = []
        for t in texts:
            if not t:
                continue
            s = json.dumps(t, default=str).lower() if not isinstance(t, str) else t.lower()
            for marker, label in cls.HOSTNAMES.items():
                if marker in s and label not in found:
                    found.append(label)
        return found

    @classmethod
    def is_persistent_id(cls, value: Optional[Any]) -> bool:
        if not value:
            return False
        s = str(value).lower()
        return any(host in s for host in ("doi.org", "hdl.handle.net", "n2t.net", "ark:", "/ark/", "purl.org"))


class AccessionDetector:
    """Pattern-match specialist-repo accession prefixes inside contentUrl
    strings (LEARNINGS.md item 3 + UpdatesNeeded.md 'Domain Appropriate')."""

    PATTERNS: ClassVar[Dict[str, Tuple[str, ...]]] = {
        "GEO": ("GSE", "GDS"),
        "SRA": ("SRR", "SRX", "SRP", "PRJ"),
        "PRIDE": ("PXD",),
        "MassIVE": ("MSV",),
        "BioStudies": ("S-BSST",),
        "dbGaP": ("phs",),
        "EGA": ("EGAS", "EGAD"),
        "ENA": ("ERR", "ERX", "ERP"),
        "ArrayExpress": ("E-MTAB", "E-GEOD"),
    }

    @classmethod
    def detect(cls, contentUrl: Any) -> List[Tuple[str, str]]:
        if not contentUrl:
            return []
        urls = contentUrl if isinstance(contentUrl, list) else [contentUrl]
        found: List[Tuple[str, str]] = []
        for u in urls:
            if not isinstance(u, str):
                continue
            for repo, prefixes in cls.PATTERNS.items():
                if any(p in u for p in prefixes):
                    found.append((repo, u[:120]))
                    break
        return found


class OntologyDetector:
    """Count and sample ontology IRIs across entities."""

    MARKERS: ClassVar[Tuple[str, ...]] = (
        "meshb.nlm.nih.gov",
        "nlm.nih.gov/mesh",
        "purl.obolibrary.org",
        "edamontology.org",
        "ncithesaurus",
        "ncit",
        "geneontology.org",
    )

    @classmethod
    def count(cls, entities: List[dict]) -> int:
        n = 0
        for e in entities:
            blob = json.dumps(e, default=str).lower()
            if any(m in blob for m in cls.MARKERS):
                n += 1
        return n

    @classmethod
    def samples(cls, entities: List[dict], max_samples: int = 10) -> List[str]:
        out: List[str] = []
        for e in entities[:5000]:
            blob = json.dumps(e, default=str)
            lower = blob.lower()
            for m in cls.MARKERS:
                if m in lower:
                    idx = lower.find(m)
                    out.append(blob[max(0, idx - 5):idx + 80])
                    if len(out) >= max_samples:
                        return out
                    break
        return out


class SchemaStandardDetector:
    """Detect standard-conformance signals in Schema entities.

    LEARNINGS.md item 1 (hard to find but present): Schemas in CM4AI carry
    a ``$schema`` reference to JSON Schema Draft 2020-12 and use
    Frictionless-style keys (``separator``, ``header``, ``required``,
    ``properties``). Treat both as standard-reference signals.
    """

    JSON_SCHEMA_RE: ClassVar[re.Pattern] = re.compile(
        r"json-schema\.org/draft/?[0-9\-]*/?schema", re.IGNORECASE
    )
    FRICTIONLESS_KEYS: ClassVar[Tuple[str, ...]] = ("separator", "header", "required", "properties")
    STANDARD_STRINGS: ClassVar[Tuple[str, ...]] = (
        "schema.org", "w3id.org/evi", "frictionless", "json-schema", "loinc",
        "omop", "ga4gh", "fhir", "datacite", "dcat",
    )

    @classmethod
    def schema_references_standard(cls, schema_entity: dict) -> bool:
        blob = json.dumps(schema_entity, default=str)
        lower = blob.lower()
        if cls.JSON_SCHEMA_RE.search(blob):
            return True
        if any(s in lower for s in cls.STANDARD_STRINGS):
            return True
        if "conformsto" in lower or "schemaversion" in lower:
            return True
        return False

    @classmethod
    def count(cls, schemas: List[dict]) -> int:
        return sum(1 for s in schemas if cls.schema_references_standard(s))


# ---------------------------------------------------------------------------
# Format buckets
# ---------------------------------------------------------------------------

PUBLISHED_FORMATS = {
    "csv", "tsv", "parquet", "hdf5", "h5", "h5ad", "fits", "nifti", "bam", "sam",
    "fastq", "fastq.gz", "json", "jsonl", "ndjson", "zarr", "image/jpeg",
    "image/png", "image/tiff", "tiff", "tif", "wav", "mp3", "flac", "txt",
    "xml", "html", "pdf", "yaml", "yml", "rdf", "ttl", "owl",
}
PROPRIETARY_FORMATS = {".d", ".d directory group", "raw", ".raw"}
SOFTWARE_RUNTIME_FORMATS = {"unknown", "executable"}

# Tabular / structured-data formats — the ones a tabular schema can plausibly
# describe (column types, units, missingness). Images, instrument-vendor
# directories, single-record binaries (BAM/FASTQ/NIfTI/etc.), and audio
# deliberately do NOT belong here: they don't carry a tabular schema and
# shouldn't drag down the 0.c / 2.c "datasets with schema" denominator.
TABULAR_FORMATS = {
    "csv", "tsv", "parquet", "h5ad", "jsonl", "ndjson",
    "arrow", "feather", "xlsx", "xls", "ods", "orc", "avro",
}


def is_tabular_dataset(entity: dict) -> bool:
    """True iff the dataset's declared format(s) include a tabular container.
    Datasets without a declared format return False — without a format we
    can't claim a tabular schema would be appropriate."""
    fmt = get_format(entity)
    if not fmt:
        return False
    candidates = fmt if isinstance(fmt, list) else [fmt]
    for f in candidates:
        key = str(f).strip().lower().lstrip(".")
        if key in TABULAR_FORMATS:
            return True
    return False


def _format_buckets(format_distribution: dict) -> Tuple[int, int, dict]:
    """Return (published_count, proprietary_count, software_runtime_distribution).
    Software-runtime values (``unknown``, ``executable``) are pulled out so
    they don't pollute data-format reporting (LEARNINGS.md surprise note)."""
    published = 0
    proprietary = 0
    runtime_dist: Dict[str, int] = {}
    for fmt, count in format_distribution.items():
        key = str(fmt).strip().lower()
        if key in SOFTWARE_RUNTIME_FORMATS:
            runtime_dist[fmt] = runtime_dist.get(fmt, 0) + count
        elif key in PUBLISHED_FORMATS:
            published += count
        elif key in PROPRIETARY_FORMATS:
            proprietary += count
    return published, proprietary, runtime_dist


# ---------------------------------------------------------------------------
# Aggregate stats — feeds most rubrics
# ---------------------------------------------------------------------------

# Fields whose values are provenance-link arrays that can explode to hundreds
# of items on real crates. We clip these in sample dicts so the LLM prompt
# stays in budget.
_HEAVY_LIST_FIELDS = frozenset((
    "generated", "prov:generated", "evi:outputs", "outputs",
    "used", "prov:used", "evi:inputs", "inputs",
    "usedDataset", "usedSoftware", "usedSample", "usedInstrument",
    "hasPart",
))


def _clip_heavy_lists(d: dict, max_items: int = 3) -> dict:
    """Return a copy of d where heavy provenance-link arrays are clipped to
    max_items, with a truncation marker indicating how many more were dropped."""
    out: dict = {}
    for k, v in d.items():
        if k in _HEAVY_LIST_FIELDS and isinstance(v, list) and len(v) > max_items:
            kept: list = list(v[:max_items])
            kept.append({"__truncated__": f"{len(v) - max_items} more (total {len(v)})"})
            out[k] = kept
        else:
            out[k] = v
    return out


def _stratify(by_source: Dict[str, List[dict]], max_total: int) -> List[dict]:
    """Round-robin pick from per-source buckets until max_total reached.

    Ensures samples surface a variety of sub-crates instead of being drained
    from the first one encountered.
    """
    result: List[dict] = []
    queues = {src: list(items) for src, items in by_source.items() if items}
    while queues and len(result) < max_total:
        for src in list(queues):
            if len(result) >= max_total:
                break
            result.append(queues[src].pop(0))
            if not queues[src]:
                del queues[src]
    return result


SAMPLES_NOTE = (
    "Samples are stratified across sub-crates and heavy provenance-link arrays "
    "(generated, prov:used, usedDataset, etc.) are clipped to 3 items with a "
    "__truncated__ marker. These are illustrative examples drawn from the crate "
    "graph — not exhaustive — and may not be fully representative."
)

# Per-source bucket caps and final sample-count targets per kind.
_SAMPLE_TARGETS = {
    "dataset": (4, 12),
    "software": (3, 8),
    "schema": (2, 5),
    "computation": (3, 8),
    "experiment": (2, 4),
    "instrument": (2, 3),
    "sample": (2, 4),
}


def aggregate_stats(bundle: ReleaseBundle) -> Dict[str, Any]:
    counts = Counter()
    formats = Counter()
    protocols = Counter()
    distribution_link_count = 0
    api_link: Optional[str] = None
    summary_stats_count = 0
    summary_stats_samples: List[dict] = []

    datasets_with_schema = 0
    tabular_dataset_count = 0
    tabular_datasets_with_schema = 0
    tabular_datasets_with_summary_stats = 0
    tabular_datasets_with_size = 0
    datasets_with_hash = 0
    datasets_with_url = 0
    datasets_with_size = 0
    datasets_embargoed = 0
    software_with_url = 0
    software_with_version = 0
    software_in_archive = 0
    software_with_hash = 0
    schemas_with_conforms = 0
    schemas_with_standard_ref = 0
    computation_with_software = 0
    computation_with_io = 0
    entities_with_prov = 0
    experiment_with_io = 0

    # Per-source buckets — stratified across sub-crates at return time.
    dataset_by_src: Dict[str, List[dict]] = defaultdict(list)
    software_by_src: Dict[str, List[dict]] = defaultdict(list)
    schema_by_src: Dict[str, List[dict]] = defaultdict(list)
    computation_by_src: Dict[str, List[dict]] = defaultdict(list)
    experiment_by_src: Dict[str, List[dict]] = defaultdict(list)
    instrument_by_src: Dict[str, List[dict]] = defaultdict(list)
    sample_by_src: Dict[str, List[dict]] = defaultdict(list)
    source_by_id = bundle.source_by_id

    domain_repo_indicators: List[Dict[str, str]] = []
    seen_accessions: set = set()

    if has_provenance_link(bundle.root_entity):
        entities_with_prov += 1

    for e in bundle.entities:
        if is_dataset(e):
            counts["dataset"] += 1
            tabular = is_tabular_dataset(e)
            if tabular:
                tabular_dataset_count += 1
            if has_hash(e):
                datasets_with_hash += 1
            if get_dataset_schema_link(e):
                datasets_with_schema += 1
                if tabular:
                    tabular_datasets_with_schema += 1
            # A Dataset is "characterized" if it either links a hasSummaryStatistics
            # entity, OR carries the lifted per-Dataset fields directly
            # (rowCount/columnCount/contentSize/sampleSize), since those now live
            # on Dataset itself per fairscape_models >= 1.1.4.
            has_direct_stats_field = any(
                e.get(k) is not None for k in ("rowCount", "columnCount", "contentSize", "sampleSize", "size")
            )
            if e.get("hasSummaryStatistics"):
                summary_stats_count += 1
                if tabular:
                    tabular_datasets_with_summary_stats += 1
                if len(summary_stats_samples) < 5:
                    summary_stats_samples.append({
                        k: e.get(k) for k in (
                            "@id", "name", "format", "hasSummaryStatistics",
                            "rowCount", "columnCount", "contentSize", "sampleSize",
                        ) if e.get(k) is not None
                    })
            elif has_direct_stats_field and len(summary_stats_samples) < 5:
                summary_stats_samples.append({
                    k: e.get(k) for k in (
                        "@id", "name", "format",
                        "rowCount", "columnCount", "contentSize", "sampleSize",
                    ) if e.get(k) is not None
                })
            if has_direct_stats_field:
                datasets_with_size += 1
                if tabular:
                    tabular_datasets_with_size += 1
            if is_embargoed(e):
                datasets_embargoed += 1
            url = first_present(e, "contentUrl", "url")
            if url:
                datasets_with_url += 1
                urls = url if isinstance(url, list) else [url]
                for u in urls:
                    if not isinstance(u, str):
                        continue
                    distribution_link_count += 1
                    if "://" in u:
                        scheme = u.split("://", 1)[0]
                        protocols[scheme] += 1
                    if api_link is None and "/api" in u:
                        api_link = u
                # Accession scan (LEARNINGS.md 5.b fix)
                for repo, sample_url in AccessionDetector.detect(url):
                    key = (repo, sample_url[:32])
                    if key not in seen_accessions:
                        seen_accessions.add(key)
                        domain_repo_indicators.append({"repo": repo, "url": sample_url})
                        if len(domain_repo_indicators) >= 12:
                            pass  # collect a few more but cap reporting later
            fmt = get_format(e)
            if fmt:
                formats[str(fmt)] += 1
            src = source_by_id.get(e.get("@id"), "unknown")
            if len(dataset_by_src[src]) < _SAMPLE_TARGETS["dataset"][0]:
                sample = {
                    k: e.get(k) for k in
                    ("@id", "name", "description", "format", "contentUrl", "contentSize", "keywords", "hasSummaryStatistics")
                    if e.get(k)
                }
                dataset_by_src[src].append(_clip_heavy_lists(sample))

        elif is_software(e):
            counts["software"] += 1
            url = first_present(e, "url", "codeRepository", "contentUrl", "@id")
            if url:
                software_with_url += 1
                u = url if isinstance(url, str) else (url[0] if isinstance(url, list) else "")
                if ArchiveDetector.detect(u):
                    # Plain GitHub / vendor pages aren't sustainable archives;
                    # only count Zenodo / Software Heritage / Figshare / OSF / Dryad / DataCite-registered.
                    sustainable = ("zenodo", "softwareheritage", "figshare", "osf.io", "dryad", "doi.org")
                    if any(host in u.lower() for host in sustainable):
                        software_in_archive += 1
            if first_present(e, "version", "versionTag"):
                software_with_version += 1
            if has_hash(e):
                software_with_hash += 1
            fmt = get_format(e)
            if fmt:
                formats[str(fmt)] += 1
            src = source_by_id.get(e.get("@id"), "unknown")
            if len(software_by_src[src]) < _SAMPLE_TARGETS["software"][0]:
                sample = {
                    k: e.get(k) for k in
                    ("@id", "name", "url", "codeRepository", "contentUrl", "version", "description", "format")
                    if e.get(k)
                }
                software_by_src[src].append(_clip_heavy_lists(sample))

        elif is_schema(e):
            counts["schema"] += 1
            if SchemaStandardDetector.schema_references_standard(e):
                schemas_with_standard_ref += 1
            blob_lower = json.dumps(e, default=str).lower()
            if "conformsto" in blob_lower or "schemaversion" in blob_lower:
                schemas_with_conforms += 1
            src = source_by_id.get(e.get("@id"), "unknown")
            if len(schema_by_src[src]) < _SAMPLE_TARGETS["schema"][0]:
                sample = {
                    k: e.get(k) for k in ("@id", "name", "description", "conformsTo", "schemaVersion")
                    if e.get(k)
                }
                schema_by_src[src].append(_clip_heavy_lists(sample))

        elif is_computation(e):
            counts["computation"] += 1
            if get_used_software(e):
                computation_with_software += 1
            if get_inputs(e) and get_outputs(e):
                computation_with_io += 1
            src = source_by_id.get(e.get("@id"), "unknown")
            if len(computation_by_src[src]) < _SAMPLE_TARGETS["computation"][0]:
                sample = {
                    k: e.get(k) for k in
                    ("@id", "name", "description", "usedSoftware", "usedDataset", "used",
                     "generated", "prov:used", "prov:generated")
                    if e.get(k)
                }
                computation_by_src[src].append(_clip_heavy_lists(sample))

        elif is_experiment(e):
            counts["experiment"] += 1
            if get_inputs(e) and get_outputs(e):
                experiment_with_io += 1
            src = source_by_id.get(e.get("@id"), "unknown")
            if len(experiment_by_src[src]) < _SAMPLE_TARGETS["experiment"][0]:
                sample = {
                    k: e.get(k) for k in ("@id", "name", "experimentType", "usedInstrument", "usedSample", "generated")
                    if e.get(k)
                }
                experiment_by_src[src].append(_clip_heavy_lists(sample))

        elif is_sample(e):
            counts["sample"] += 1
            src = source_by_id.get(e.get("@id"), "unknown")
            if len(sample_by_src[src]) < _SAMPLE_TARGETS["sample"][0]:
                sample_by_src[src].append({k: e.get(k) for k in ("@id", "name", "description") if e.get(k)})

        elif is_instrument(e):
            counts["instrument"] += 1
            src = source_by_id.get(e.get("@id"), "unknown")
            if len(instrument_by_src[src]) < _SAMPLE_TARGETS["instrument"][0]:
                instrument_by_src[src].append({k: e.get(k) for k in ("@id", "name", "description", "manufacturer") if e.get(k)})

        if has_provenance_link(e):
            entities_with_prov += 1

    return {
        "total_entities": len(bundle.entities),
        "counts_by_kind": dict(counts),
        "format_distribution": dict(formats),
        "distinct_protocols": sorted(protocols.keys()),
        "distribution_link_count": distribution_link_count,
        "api_link": api_link,
        "datasets_with_hash": datasets_with_hash,
        "datasets_with_schema": datasets_with_schema,
        "tabular_dataset_count": tabular_dataset_count,
        "tabular_datasets_with_schema": tabular_datasets_with_schema,
        "tabular_datasets_with_summary_stats": tabular_datasets_with_summary_stats,
        "tabular_datasets_with_size": tabular_datasets_with_size,
        "datasets_with_url": datasets_with_url,
        "datasets_with_size": datasets_with_size,
        "datasets_with_summary_stats": summary_stats_count,
        "datasets_embargoed": datasets_embargoed,
        "summary_stats_samples": summary_stats_samples,
        "software_with_url": software_with_url,
        "software_with_version": software_with_version,
        "software_in_archive": software_in_archive,
        "software_with_hash": software_with_hash,
        "schemas_with_conforms_to": schemas_with_conforms,
        "schemas_with_standard_ref": schemas_with_standard_ref,
        "computation_with_software": computation_with_software,
        "computation_with_io": computation_with_io,
        "experiment_with_io": experiment_with_io,
        "entities_with_provenance_link": entities_with_prov,
        "domain_repo_indicators": domain_repo_indicators,
        "samples_note": SAMPLES_NOTE,
        "sample_sources": sorted(
            {s for s in source_by_id.values()}
        ),
        "samples": {
            "dataset": _stratify(dataset_by_src, _SAMPLE_TARGETS["dataset"][1]),
            "software": _stratify(software_by_src, _SAMPLE_TARGETS["software"][1]),
            "schema": _stratify(schema_by_src, _SAMPLE_TARGETS["schema"][1]),
            "computation": _stratify(computation_by_src, _SAMPLE_TARGETS["computation"][1]),
            "experiment": _stratify(experiment_by_src, _SAMPLE_TARGETS["experiment"][1]),
            "instrument": _stratify(instrument_by_src, _SAMPLE_TARGETS["instrument"][1]),
            "sample": _stratify(sample_by_src, _SAMPLE_TARGETS["sample"][1]),
        },
    }


# ---------------------------------------------------------------------------
# Root-level helpers
# ---------------------------------------------------------------------------

HL7_CONFIDENTIALITY_CODES = {"unrestricted", "normal", "restricted", "very restricted", "u", "n", "r", "v"}


def confidentiality_is_categorical(value: Any) -> bool:
    """LEARNINGS.md 0.b fix: any short categorical string counts as
    machine-interpretable; only flag free-text prose."""
    if value is None or value == "":
        return False
    if not isinstance(value, str):
        return True
    s = value.strip()
    return len(s) <= 32 and "\n" not in s


def confidentiality_is_hl7(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    return value.strip().lower() in HL7_CONFIDENTIALITY_CODES


def is_resolvable_license(value: Optional[Any]) -> bool:
    if not value:
        return False
    s = str(value).lower()
    if s.startswith("http") or s.startswith("https"):
        return True
    spdx_prefixes = ("cc-", "cc0", "mit", "apache", "gpl", "bsd", "mpl", "lgpl", "0bsd")
    return any(s.startswith(p) for p in spdx_prefixes)


def is_license_cc0(value: Optional[Any]) -> bool:
    return "cc0" in str(value or "").lower() or "publicdomain/zero" in str(value or "").lower()


def author_orcid_coverage(author_value: Any) -> Tuple[int, int]:
    """Return (author_count, authors_with_orcid_count). Handles
    semicolon-separated strings (CM4AI's current shape) as well as
    list-of-Person dicts."""
    if isinstance(author_value, str):
        names = [a.strip() for a in author_value.split(";") if a.strip()]
        return len(names), 0
    if isinstance(author_value, list):
        author_count = len(author_value)
        with_orcid = 0
        for a in author_value:
            if isinstance(a, dict):
                blob = (str(a.get("@id", "")) + " " + str(a.get("identifier", ""))).lower()
                if "orcid.org" in blob:
                    with_orcid += 1
            elif isinstance(a, str) and "orcid.org" in a.lower():
                with_orcid += 1
        return author_count, with_orcid
    if isinstance(author_value, dict):
        blob = (str(author_value.get("@id", "")) + " " + str(author_value.get("identifier", ""))).lower()
        return 1, (1 if "orcid.org" in blob else 0)
    return 0, 0


def publisher_has_ror(publisher: Any) -> bool:
    if isinstance(publisher, dict):
        s = json.dumps(publisher, default=str).lower()
    else:
        s = str(publisher or "").lower()
    return "ror.org" in s


def scan_irb_refs(root: dict) -> List[str]:
    refs: List[str] = []
    for key in ("irb", "irbProtocolId", "ethicalReview", "humanSubjectExemption"):
        v = root.get(key)
        if v:
            refs.append(f"{key}: {str(v)[:80]}")
    if not refs:
        blob = json.dumps(root, default=str)
        if re.search(r"\bIRB\b|\bprotocol\b", blob, re.IGNORECASE):
            refs.append("IRB or protocol reference in metadata text")
    return refs


def privacy_protection_text(root: dict) -> Optional[str]:
    val = root.get("rai:personalSensitiveInformation")
    if val:
        if isinstance(val, list):
            return ", ".join(str(v) for v in val)
        return str(val)
    return None


def find_datasheet_entity(bundle: ReleaseBundle) -> Optional[dict]:
    for e in bundle.entities:
        blob = " ".join(str(e.get(k, "")) for k in ("name", "description")).lower()
        if "datasheet" in blob or "data sheet" in blob:
            return {k: e.get(k) for k in ("@id", "name", "description", "@type", "encodingFormat", "url") if e.get(k)}
    return None


def find_datasheet_file(bundle: ReleaseBundle) -> Optional[str]:
    """LEARNINGS.md 3.a: look for a sibling datasheet file when none is
    declared in the @graph."""
    for candidate in ("ro-crate-datasheet.html", "datasheet.html", "datasheet.pdf", "datasheet.md"):
        if (bundle.crate_dir / candidate).exists():
            return candidate
    # Fallback glob — any file with 'datasheet' in the name
    for p in bundle.crate_dir.iterdir():
        if "datasheet" in p.name.lower() and p.suffix.lower() in {".html", ".pdf", ".md"}:
            return p.name
    return None


def scan_split_text(root: dict) -> Optional[str]:
    blob = json.dumps(root, default=str).lower()
    if any(t in blob for t in ("train/test", "training set", "validation set", "test split", "holdout", "train/val")):
        return "split-language detected in root metadata"
    return None


def count_split_datasets(entities: List[dict]) -> int:
    n = 0
    for e in entities:
        if not is_dataset(e):
            continue
        name = (str(e.get("name") or "") + " " + " ".join(type_tokens(e))).lower()
        if any(s in name for s in ("training", "validation", "test ", "holdout", " train ", " test")):
            n += 1
    return n


def example_record_indicators(entities: List[dict], cap: int = 6) -> List[str]:
    out: List[str] = []
    for e in entities:
        name = str(e.get("name") or "").lower()
        if "example" in name or "sample " in name or name.startswith("sample"):
            if e.get("@id"):
                out.append(e["@id"])
        if len(out) >= cap:
            break
    return out


def root_conforms_to(bundle: ReleaseBundle) -> List[Any]:
    """Combine conformsTo from the root entity and from the descriptor."""
    out: List[Any] = []
    out.extend(as_list(bundle.root_entity.get("conformsTo")))
    main_graph = bundle.main_crate.get("@graph", [])
    if main_graph:
        out.extend(as_list(main_graph[0].get("conformsTo")))
    # Deduplicate while preserving order
    seen = set()
    dedup: List[Any] = []
    for x in out:
        key = json.dumps(x, default=str, sort_keys=True)
        if key not in seen:
            seen.add(key)
            dedup.append(x)
    return dedup


def build_by_id(entities: List[dict]) -> Dict[str, dict]:
    """Map @id → entity for every dict in the merged graph that carries one."""
    return {
        e["@id"]: e
        for e in entities
        if isinstance(e, dict) and isinstance(e.get("@id"), str)
    }


def resolve_ref(value: Any, by_id: Dict[str, dict], _depth: int = 3) -> Any:
    """Inline-expand ``{"@id": X}`` reference stubs against the merged graph.

    Flat-RO-Crate compliant crates carry Persons, DefinedTerms, and
    Organizations as top-level @graph entries and reference them from the root
    via single-key ``{"@id": "..."}`` stubs. Without resolution, the LLM
    payload sees opaque ids instead of names/affiliations and qualitative
    scoring drops.

    Recurses into lists and into the resolved entity's fields (so a Person's
    ``affiliation`` stub also expands). Strings, numbers, and dicts that
    already carry more than just ``@id`` pass through unchanged. ``_depth``
    bounds recursion as a cycle guard.
    """
    if _depth <= 0:
        return value
    if isinstance(value, list):
        return [resolve_ref(v, by_id, _depth) for v in value]
    if isinstance(value, dict) and list(value.keys()) == ["@id"]:
        target = by_id.get(value["@id"])
        if target is not None:
            return {k: resolve_ref(v, by_id, _depth - 1) for k, v in target.items()}
    return value


def root_summary(bundle: ReleaseBundle, by_id: Optional[Dict[str, dict]] = None) -> dict:
    root = bundle.root_entity
    desc = root.get("description")
    if by_id is None:
        by_id = build_by_id(bundle.entities)
    return {
        "name": root.get("name"),
        "description_excerpt": (str(desc)[:280] + "...") if desc else None,
        "identifier": root.get("identifier") or root.get("@id"),
        "license": root.get("license"),
        "publisher": resolve_ref(root.get("publisher"), by_id),
        "datePublished": root.get("datePublished"),
        "version": root.get("version"),
        "principalInvestigator": resolve_ref(root.get("principalInvestigator"), by_id),
        "contactEmail": root.get("contactEmail"),
        "confidentialityLevel": root.get("confidentialityLevel"),
        "sub_crate_count": len(bundle.sub_crates),
    }


# ---------------------------------------------------------------------------
# Rubric extractors
# ---------------------------------------------------------------------------


class RubricExtractor(ABC):
    """Base class — one subclass per rubric YAML. Each ``extract()`` returns
    the dict shape declared by the rubric's ``extractor_inputs.properties``."""

    rubric_id: ClassVar[str] = ""
    rubric_slug: ClassVar[str] = ""

    @abstractmethod
    def extract(self, ctx: "ExtractContext") -> dict:
        ...


class ExtractContext:
    """Holds the bundle plus precomputed aggregates so each extractor doesn't
    have to walk the graph from scratch. Built once per call to
    ``extract_all_inputs``."""

    def __init__(self, bundle: ReleaseBundle):
        self.bundle = bundle
        self.root = bundle.root_entity
        self.by_id = build_by_id(bundle.entities)
        self.stats = aggregate_stats(bundle)
        self.context_namespaces = bundle.context_namespaces
        self.recognized_standards = StandardsDetector.detect(bundle.context_namespaces)

        # Prefer pre-aggregated evi:* counts on root, fall back to walked counts.
        self.dataset_count = self.root.get("evi:datasetCount") or self.stats["counts_by_kind"].get("dataset", 0)
        self.software_count = self.root.get("evi:softwareCount") or self.stats["counts_by_kind"].get("software", 0)
        self.schema_count = self.root.get("evi:schemaCount") or self.stats["counts_by_kind"].get("schema", 0)
        self.computation_count = self.stats["counts_by_kind"].get("computation", 0)
        self.experiment_count = self.stats["counts_by_kind"].get("experiment", 0)
        self.instrument_count = self.stats["counts_by_kind"].get("instrument", 0)
        self.sample_count = self.stats["counts_by_kind"].get("sample", 0)
        self.total_entities = self.root.get("evi:totalEntities") or self.stats["total_entities"]
        self.total_size_bytes = self.root.get("evi:totalContentSizeBytes")

        entities_with_checksums = self.root.get("evi:entitiesWithChecksums")
        if entities_with_checksums is None:
            entities_with_checksums = self.stats["datasets_with_hash"] + self.stats["software_with_hash"]
        self.entities_with_checksums = entities_with_checksums

        # License / access
        self.license_value = self.root.get("license") or self.root.get("dataLicense")
        self.conditions_of_access = self.root.get("conditionsOfAccess")
        self.prohibited_uses = (
            self.root.get("prohibitedUses")
            or get_additional_property(self.root, "Prohibited Uses")
            or self.root.get("usageInfo")
        )

        # Actors / governance. Resolve {"@id": ...} stubs against the merged
        # graph so downstream rubrics see Person names/affiliations rather
        # than opaque references when the crate is flat-RO-Crate compliant.
        self.publisher = resolve_ref(self.root.get("publisher"), self.by_id)
        self.governance_committee = resolve_ref(
            self.root.get("dataGovernanceCommittee")
            or get_additional_property(self.root, "Data Governance Committee"),
            self.by_id,
        )
        self.human_subject = (
            self.root.get("humanSubjectResearch")
            or get_additional_property(self.root, "Human Subject Research")
            or get_additional_property(self.root, "Human Subject")
        )

        # Archive indicators (publisher + identifier + description)
        self.archive_indicators = ArchiveDetector.detect(
            self.publisher,
            self.conditions_of_access,
            self.root.get("description"),
            self.root.get("identifier"),
        )

        # Author / ORCID coverage
        author_count, authors_with_orcid = author_orcid_coverage(self.root.get("author"))
        self.author_count = author_count
        self.authors_with_orcid = authors_with_orcid
        self.publisher_has_ror = publisher_has_ror(self.publisher)

        # Datasheet
        self.datasheet_entity = find_datasheet_entity(bundle)
        self.datasheet_file = find_datasheet_file(bundle)
        self.populated_section_fields = {
            "rai_dataCollection": bool(self.root.get("rai:dataCollection")),
            "rai_dataUseCases": bool(self.root.get("rai:dataUseCases")),
            "rai_dataLimitations": bool(self.root.get("rai:dataLimitations")),
            "rai_dataBiases": bool(self.root.get("rai:dataBiases")),
            "rai_dataReleaseMaintenancePlan": bool(self.root.get("rai:dataReleaseMaintenancePlan")),
            "license": bool(self.license_value),
            "dua": bool(self.conditions_of_access),
        }
        self.populated_section_count = sum(1 for v in self.populated_section_fields.values() if v)

        # Format buckets
        published, proprietary, runtime_dist = _format_buckets(self.stats["format_distribution"])
        self.published_format_count = published
        self.proprietary_format_count = proprietary
        self.software_runtime_formats = runtime_dist


# ----- 0.x — FAIRness ----------------------------------------------------------


class Findable(RubricExtractor):
    rubric_id = "0.a"
    rubric_slug = "findable"

    def extract(self, ctx: ExtractContext) -> dict:
        return {
            "root_identifier": ctx.root.get("identifier") or ctx.root.get("@id"),
            "publisher_info": ctx.publisher,
            "archive_indicators": ctx.archive_indicators,
        }


class Accessible(RubricExtractor):
    rubric_id = "0.b"
    rubric_slug = "accessible"

    def extract(self, ctx: ExtractContext) -> dict:
        cl = ctx.root.get("confidentialityLevel")
        return {
            "context_namespaces": ctx.context_namespaces,
            "recognized_vocabularies": ctx.recognized_standards,
            "confidentiality_level": cl,
            "confidentiality_level_is_categorical": confidentiality_is_categorical(cl),
            "confidentiality_level_is_hl7_code": confidentiality_is_hl7(cl),
            "conditions_of_access": ctx.conditions_of_access,
        }


class Interoperable(RubricExtractor):
    rubric_id = "0.c"
    rubric_slug = "interoperable"

    def extract(self, ctx: ExtractContext) -> dict:
        tabular = ctx.stats["tabular_dataset_count"]
        tabular_with_schema = ctx.stats["tabular_datasets_with_schema"]
        return {
            "context_namespaces": ctx.context_namespaces,
            "schema_count": ctx.schema_count,
            # `dataset_count` / `datasets_with_schema_count` are tabular-scoped
            # to match the rubric's "every tabular / structured Dataset linked
            # to a schema" wording. Imaging files, instrument-vendor `.d`
            # directories, BAM/FASTQ, etc. can't carry a tabular schema and
            # would otherwise dominate the denominator.
            "dataset_count": tabular,
            "datasets_with_schema_count": tabular_with_schema,
            "dataset_count_all": ctx.dataset_count,
            "non_tabular_dataset_count": ctx.dataset_count - tabular,
            "datasets_with_schema_all_count": ctx.stats["datasets_with_schema"],
            "schemas_referencing_standards_count": ctx.stats["schemas_with_standard_ref"],
            "formats_with_published_spec_count": ctx.published_format_count,
            "format_distribution": ctx.stats["format_distribution"],
        }


class Reusable(RubricExtractor):
    rubric_id = "0.d"
    rubric_slug = "reusable"

    def extract(self, ctx: ExtractContext) -> dict:
        return {
            "license_value": ctx.license_value,
            "license_is_resolvable": is_resolvable_license(ctx.license_value),
            "license_is_cc0": is_license_cc0(ctx.license_value),
            "conditions_of_access": ctx.conditions_of_access,
            "prohibited_uses": ctx.prohibited_uses,
        }


# ----- 1.x — Provenance --------------------------------------------------------


class Transparent(RubricExtractor):
    rubric_id = "1.a"
    rubric_slug = "transparent"

    def extract(self, ctx: ExtractContext) -> dict:
        return {
            "dataset_count": ctx.dataset_count,
            "experiment_count": ctx.experiment_count,
            "instrument_count": ctx.instrument_count,
            "computation_count": ctx.computation_count,
            "software_count": ctx.software_count,
            "sample_count": ctx.sample_count,
            "dataset_samples": ctx.stats["samples"]["dataset"][:10],
            "experiment_samples": ctx.stats["samples"]["experiment"],
            "instrument_samples": ctx.stats["samples"]["instrument"],
            "samples_note": ctx.stats["samples_note"],
            "root_description": ctx.root.get("description"),
            "root_keywords": ctx.root.get("keywords") or [],
        }


class Traceable(RubricExtractor):
    rubric_id = "1.b"
    rubric_slug = "traceable"

    def extract(self, ctx: ExtractContext) -> dict:
        # LEARNINGS.md fix: count only Computations here (not Experiments).
        return {
            "computation_count": ctx.computation_count,
            "computation_with_software_link_count": ctx.stats["computation_with_software"],
            "computation_with_io_count": ctx.stats["computation_with_io"],
            "experiment_count": ctx.experiment_count,
            "experiment_with_io_count": ctx.stats["experiment_with_io"],
            "dataset_count": ctx.dataset_count,
            "software_count": ctx.software_count,
            "computation_samples": ctx.stats["samples"]["computation"],
            "samples_note": ctx.stats["samples_note"],
        }


class Interpretable(RubricExtractor):
    rubric_id = "1.c"
    rubric_slug = "interpretable"

    def extract(self, ctx: ExtractContext) -> dict:
        return {
            "software_count": ctx.software_count,
            "software_with_link_count": ctx.stats["software_with_url"],
            "software_in_sustainable_archive_count": ctx.stats["software_in_archive"],
            "software_with_version_count": ctx.stats["software_with_version"],
            "software_samples": ctx.stats["samples"]["software"],
            "samples_note": ctx.stats["samples_note"],
        }


class KeyActorsIdentified(RubricExtractor):
    rubric_id = "1.d"
    rubric_slug = "key-actors-identified"

    def extract(self, ctx: ExtractContext) -> dict:
        root = ctx.root
        return {
            "author_count": ctx.author_count,
            "authors_with_orcid_count": ctx.authors_with_orcid,
            "publisher_present": bool(ctx.publisher),
            "publisher_has_ror": ctx.publisher_has_ror,
            "principal_investigator_present": bool(root.get("principalInvestigator")),
            "root_actors": {
                "author": resolve_ref(root.get("author"), ctx.by_id),
                "publisher": ctx.publisher,
                "principalInvestigator": resolve_ref(root.get("principalInvestigator"), ctx.by_id),
                "contactEmail": root.get("contactEmail"),
                "ethicalReview": root.get("ethicalReview"),
                "governance": ctx.governance_committee,
                "funder": resolve_ref(root.get("funder"), ctx.by_id),
                "about": resolve_ref(root.get("about"), ctx.by_id),
            },
        }


# ----- 2.x — Characterization --------------------------------------------------


class Semantics(RubricExtractor):
    rubric_id = "2.a"
    rubric_slug = "semantics"

    def extract(self, ctx: ExtractContext) -> dict:
        desc = ctx.root.get("description")
        return {
            "root_description": desc,
            "root_description_length": len(str(desc or "")),
            "root_keywords": ctx.root.get("keywords") or [],
            "dataset_count": ctx.dataset_count,
            "dataset_samples": ctx.stats["samples"]["dataset"][:10],
            "ontology_term_count": OntologyDetector.count(ctx.bundle.entities),
            "ontology_term_samples": OntologyDetector.samples(ctx.bundle.entities),
            "samples_note": ctx.stats["samples_note"],
        }


class Statistics(RubricExtractor):
    rubric_id = "2.b"
    rubric_slug = "statistics"

    def extract(self, ctx: ExtractContext) -> dict:
        # Statistics coverage is tabular-scoped: image directories, vendor
        # binaries (.d / .raw), BAM/FASTQ, etc. can't plausibly carry row/column
        # counts or summary distributions, so they shouldn't drag the denominator
        # down. Both totals are reported so the grader can sanity-check.
        tabular = ctx.stats["tabular_dataset_count"]
        return {
            "dataset_count": tabular,
            "tabular_dataset_count": tabular,
            "dataset_count_all": ctx.dataset_count,
            "non_tabular_dataset_count": ctx.dataset_count - tabular,
            "datasets_with_summary_stats_count": (
                ctx.root.get("evi:entitiesWithSummaryStats")
                if ctx.root.get("evi:entitiesWithSummaryStats") is not None
                else ctx.stats["tabular_datasets_with_summary_stats"]
            ),
            "datasets_with_summary_stats_all_count": ctx.stats["datasets_with_summary_stats"],
            "datasets_with_size_count": ctx.stats["tabular_datasets_with_size"],
            "datasets_with_size_all_count": ctx.stats["datasets_with_size"],
            "summary_stats_samples": ctx.stats["summary_stats_samples"],
            "samples_note": ctx.stats["samples_note"],
            "missing_value_convention_text": ctx.root.get("rai:dataCollectionMissingData"),
            "total_size_bytes": ctx.total_size_bytes,
        }


class Standards(RubricExtractor):
    rubric_id = "2.c"
    rubric_slug = "standards"

    def extract(self, ctx: ExtractContext) -> dict:
        tabular = ctx.stats["tabular_dataset_count"]
        tabular_with_schema = ctx.stats["tabular_datasets_with_schema"]
        return {
            "schema_count": ctx.schema_count,
            # Tabular-scoped to match the rubric's "every tabular / structured
            # Dataset linked to a schema" coverage question — see 0.c for the
            # full rationale.
            "dataset_count": tabular,
            "datasets_with_schema_count": tabular_with_schema,
            "dataset_count_all": ctx.dataset_count,
            "non_tabular_dataset_count": ctx.dataset_count - tabular,
            "datasets_with_schema_all_count": ctx.stats["datasets_with_schema"],
            "schemas_referencing_standards_count": ctx.stats["schemas_with_standard_ref"],
            "schema_samples": ctx.stats["samples"]["schema"],
            "samples_note": ctx.stats["samples_note"],
        }


class PotentialSourcesOfBias(RubricExtractor):
    rubric_id = "2.d"
    rubric_slug = "potential-sources-of-bias"

    def extract(self, ctx: ExtractContext) -> dict:
        return {
            "rai_dataBiases": ctx.root.get("rai:dataBiases"),
            "rai_dataCollectionMissingData": ctx.root.get("rai:dataCollectionMissingData"),
        }


class DataQuality(RubricExtractor):
    rubric_id = "2.e"
    rubric_slug = "data-quality"

    def extract(self, ctx: ExtractContext) -> dict:
        return {
            "rai_dataCollection": ctx.root.get("rai:dataCollection"),
            "rai_dataCollectionMissingData": ctx.root.get("rai:dataCollectionMissingData"),
        }


# ----- 3.x — Pre-model Explainability ------------------------------------------


class DataDocumentationTemplate(RubricExtractor):
    rubric_id = "3.a"
    rubric_slug = "data-documentation-template"

    def extract(self, ctx: ExtractContext) -> dict:
        return {
            "has_human_readable_datasheet": bool(ctx.datasheet_entity or ctx.datasheet_file),
            "datasheet_entity": ctx.datasheet_entity,
            "datasheet_file": ctx.datasheet_file,
            "populated_section_fields": ctx.populated_section_fields,
            "populated_section_count": ctx.populated_section_count,
            "healthsheet_fields_present": [],
        }


class FitForPurpose(RubricExtractor):
    rubric_id = "3.b"
    rubric_slug = "fit-for-purpose"

    def extract(self, ctx: ExtractContext) -> dict:
        pubs = ctx.root.get("associatedPublication") or []
        if isinstance(pubs, str):
            pubs = [pubs]
        return {
            "rai_dataUseCases": ctx.root.get("rai:dataUseCases"),
            "rai_dataLimitations": ctx.root.get("rai:dataLimitations"),
            "prohibited_uses": ctx.prohibited_uses,
            "associated_publications": pubs,
        }


class Verifiable(RubricExtractor):
    rubric_id = "3.c"
    rubric_slug = "verifiable"

    def extract(self, ctx: ExtractContext) -> dict:
        # LEARNINGS.md 3.c fix: exclude embargoed Datasets from denominator.
        total_hashable = (ctx.dataset_count + ctx.software_count) - ctx.stats["datasets_embargoed"]
        if total_hashable < 0:
            total_hashable = 0
        coverage: Optional[float] = (
            ctx.entities_with_checksums / total_hashable if total_hashable > 0 else None
        )
        return {
            "total_hashable_entities": total_hashable,
            "total_hashable_entities_raw": ctx.dataset_count + ctx.software_count,
            "entities_with_hash": ctx.entities_with_checksums,
            "datasets_embargoed_excluded": ctx.stats["datasets_embargoed"],
            "hash_coverage": coverage,
            "datasets_with_hash": ctx.stats["datasets_with_hash"],
            "software_with_hash": ctx.stats["software_with_hash"],
        }


# ----- 4.x — Ethics ------------------------------------------------------------


class EthicallyAcquired(RubricExtractor):
    rubric_id = "4.a"
    rubric_slug = "ethically-acquired"

    def extract(self, ctx: ExtractContext) -> dict:
        root = ctx.root
        return {
            "rai_dataCollection": root.get("rai:dataCollection"),
            "human_subject_research_value": ctx.human_subject,
            "ethical_review_text": root.get("ethicalReview"),
            "informed_consent": root.get("informedConsent") or root.get("d4d:informedConsent"),
            "at_risk_populations": root.get("atRiskPopulations") or root.get("d4d:atRiskPopulations"),
            "irb_or_consent_references": scan_irb_refs(root),
            "management_plan_text": root.get("rai:dataReleaseMaintenancePlan"),
        }


class EthicallyManaged(RubricExtractor):
    rubric_id = "4.b"
    rubric_slug = "ethically-managed"

    def extract(self, ctx: ExtractContext) -> dict:
        root = ctx.root
        return {
            "ethical_review_text": root.get("ethicalReview"),
            "governance_committee": ctx.governance_committee,
            "privacy_protection_text": privacy_protection_text(root),
            "confidentiality_level": root.get("confidentialityLevel"),
        }


class EthicallyDisseminated(RubricExtractor):
    rubric_id = "4.c"
    rubric_slug = "ethically-disseminated"

    def extract(self, ctx: ExtractContext) -> dict:
        root = ctx.root
        return {
            "license_value": ctx.license_value,
            "license_is_resolvable": is_resolvable_license(ctx.license_value),
            "license_is_cc0": is_license_cc0(ctx.license_value),
            "conditions_of_access": ctx.conditions_of_access,
            "prohibited_uses": ctx.prohibited_uses,
            "rai_personalSensitiveInformation": root.get("rai:personalSensitiveInformation"),
            "confidentiality_level": root.get("confidentialityLevel"),
            "contact_email": root.get("contactEmail"),
        }


class Secure(RubricExtractor):
    rubric_id = "4.d"
    rubric_slug = "secure"

    def extract(self, ctx: ExtractContext) -> dict:
        cl = ctx.root.get("confidentialityLevel")
        return {
            "confidentiality_level": cl,
            "confidentiality_level_is_categorical": confidentiality_is_categorical(cl),
            "confidentiality_level_is_hl7_code": confidentiality_is_hl7(cl),
            "rai_personalSensitiveInformation": ctx.root.get("rai:personalSensitiveInformation"),
            "deidentified": ctx.root.get("deidentified"),
        }


# ----- 5.x — Sustainability ----------------------------------------------------


class Persistent(RubricExtractor):
    rubric_id = "5.a"
    rubric_slug = "persistent"

    def extract(self, ctx: ExtractContext) -> dict:
        identifier = ctx.root.get("identifier") or ctx.root.get("@id")
        return {
            "root_identifier": identifier,
            "identifier_is_pid": ArchiveDetector.is_persistent_id(identifier),
            "publisher_info": ctx.publisher,
            "archive_indicators": ctx.archive_indicators,
        }


class DomainAppropriate(RubricExtractor):
    rubric_id = "5.b"
    rubric_slug = "domain-appropriate"

    def extract(self, ctx: ExtractContext) -> dict:
        # Distribution-link samples drawn from the dataset sample bucket
        distribution_links: List[dict] = []
        for s in ctx.stats["samples"]["dataset"][:10]:
            url = s.get("contentUrl") or s.get("url")
            if url:
                distribution_links.append({"@id": s.get("@id"), "url": url})
        return {
            "distribution_links": distribution_links,
            "domain_repo_indicators": ctx.stats["domain_repo_indicators"][:12],
            "publisher_info": ctx.publisher,
            "data_domain_hint": ctx.root.get("rai:dataCollectionType") or ctx.root.get("keywords"),
            "distinct_protocols": ctx.stats["distinct_protocols"],
        }


class WellGoverned(RubricExtractor):
    rubric_id = "5.c"
    rubric_slug = "well-governed"

    def extract(self, ctx: ExtractContext) -> dict:
        return {
            "governance_committee": ctx.governance_committee,
            "maintenance_plan_text": ctx.root.get("rai:dataReleaseMaintenancePlan"),
            "principal_investigator": resolve_ref(ctx.root.get("principalInvestigator"), ctx.by_id),
            "contact_email": ctx.root.get("contactEmail"),
        }


class Associated(RubricExtractor):
    rubric_id = "5.d"
    rubric_slug = "associated"

    def extract(self, ctx: ExtractContext) -> dict:
        # Use the walked total so numerator and denominator come from the
        # same graph traversal; root.evi:totalEntities is a pre-aggregated
        # snapshot that may differ slightly from the deduped walk.
        total = ctx.stats["total_entities"]
        with_prov = ctx.stats["entities_with_provenance_link"]
        density: Optional[float] = (with_prov / total) if total > 0 else None
        return {
            "total_entities": total,
            "total_entities_root_aggregate": ctx.root.get("evi:totalEntities"),
            "root_haspart_count": len(ctx.root.get("hasPart") or []),
            "entities_with_provenance_link_count": with_prov,
            "subcrate_count": len(ctx.bundle.sub_crates),
            "provenance_link_density": density,
        }


# ----- 6.x — Computability -----------------------------------------------------


class Standardized(RubricExtractor):
    rubric_id = "6.a"
    rubric_slug = "standardized"

    def extract(self, ctx: ExtractContext) -> dict:
        return {
            "root_conformsTo": root_conforms_to(ctx.bundle),
            "context_namespaces": ctx.context_namespaces,
            "recognized_standards": ctx.recognized_standards,
            "schemas_referencing_standards_count": ctx.stats["schemas_with_standard_ref"],
            "validation_report_present": False,
        }


class ComputationallyAccessible(RubricExtractor):
    rubric_id = "6.b"
    rubric_slug = "computationally-accessible"

    def extract(self, ctx: ExtractContext) -> dict:
        return {
            "distribution_link_count": ctx.stats["distribution_link_count"],
            "distinct_protocols": ctx.stats["distinct_protocols"],
            "api_link": ctx.stats["api_link"],
            "access_instruction_text": ctx.conditions_of_access,
            "publisher": ctx.publisher,
        }


class Portable(RubricExtractor):
    rubric_id = "6.c"
    rubric_slug = "portable"

    def extract(self, ctx: ExtractContext) -> dict:
        return {
            "format_distribution": ctx.stats["format_distribution"],
            "common_format_count": ctx.published_format_count,
            "proprietary_format_count": ctx.proprietary_format_count,
            "software_runtime_formats": ctx.software_runtime_formats,
            "container_references": [],
            "hardware_requirement_text": [],
        }


class Contextualized(RubricExtractor):
    rubric_id = "6.d"
    rubric_slug = "contextualized"

    def extract(self, ctx: ExtractContext) -> dict:
        return {
            "split_text": scan_split_text(ctx.root),
            "split_dataset_count": count_split_datasets(ctx.bundle.entities),
            "withheld_information_text": ctx.root.get("rai:dataCollectionMissingData"),
            "example_record_indicators": example_record_indicators(ctx.bundle.entities),
            "preprocessing_text": ctx.root.get("rai:dataPreprocessingProtocol"),
        }


ALL_EXTRACTORS: List[type[RubricExtractor]] = [
    Findable, Accessible, Interoperable, Reusable,
    Transparent, Traceable, Interpretable, KeyActorsIdentified,
    Semantics, Statistics, Standards, PotentialSourcesOfBias, DataQuality,
    DataDocumentationTemplate, FitForPurpose, Verifiable,
    EthicallyAcquired, EthicallyManaged, EthicallyDisseminated, Secure,
    Persistent, DomainAppropriate, WellGoverned, Associated,
    Standardized, ComputationallyAccessible, Portable, Contextualized,
]

assert len(ALL_EXTRACTORS) == 28, f"expected 28 extractors, got {len(ALL_EXTRACTORS)}"
assert len({c.rubric_id for c in ALL_EXTRACTORS}) == 28, "duplicate rubric_id"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_release(main_metadata_path: str | Path) -> ReleaseBundle:
    """Load the main crate plus every sub-crate referenced via the
    'ro-crate-metadata' field on entries of the main @graph."""
    return ReleaseBundle.load(main_metadata_path)


def extract_all_inputs(bundle: ReleaseBundle) -> Dict[str, Any]:
    """Pull every extractor_inputs payload the rubrics need.

    Returns:
        {
            "root_summary": dict,
            "stats":        dict,
            "rubric_inputs": {"0.a": {...}, ..., "6.d": {...}},
        }
    """
    ctx = ExtractContext(bundle)
    rubric_inputs: Dict[str, dict] = {}
    for cls in ALL_EXTRACTORS:
        rubric_inputs[cls.rubric_id] = cls().extract(ctx)
    return {
        "root_summary": root_summary(bundle, by_id=ctx.by_id),
        "stats": ctx.stats,
        "rubric_inputs": rubric_inputs,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: List[str]) -> int:
    if len(argv) < 2:
        print("Usage: extract.py <ro-crate-metadata.json> [output.json]")
        return 1
    bundle = load_release(argv[1])
    inputs = extract_all_inputs(bundle)
    payload = json.dumps(inputs, indent=2, default=str)
    if len(argv) > 2:
        out_path = argv[2]
        with open(out_path, "w") as f:
            f.write(payload)
        print(f"wrote {out_path}")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
