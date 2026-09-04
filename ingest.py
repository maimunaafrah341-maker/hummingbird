"""
Build the FAISS retrieval index that POST /incident searches.

    python ingest.py                 # build if the corpus changed
    python ingest.py --force         # rebuild regardless
    python ingest.py --corpus corpus/ --out vectorstore/

Standalone on purpose. This runs at build time, not request time, so it
loads its own SentenceTransformer rather than going through language.py
-- nothing here should be coupled to the running service. It reuses
language.MODEL_NAME so there is exactly one embedding model in the
project; a second one would mean a second multi-hundred-megabyte
download and an index the API could not search.

Reads the sample corpus in corpus/ and writes two files:

    vectorstore/index.faiss     the vectors
    vectorstore/metadata.json   the manifest and one record per row

The metadata sidecar is row-aligned with the index: FAISS returns
integer row ids and nothing else, so record i in "chunks" must describe
vector i. Nothing enforces that but this script, which is why both
files are written together, atomically, and never separately.
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time

import language


# ============================================================
# CONFIGURATION
# ============================================================

CORPUS_DIR = "corpus"
OUTPUT_DIR = "vectorstore"

INDEX_FILENAME = "index.faiss"
METADATA_FILENAME = "metadata.json"

# Chunk size targets, in estimated tokens. A chunk has to be small
# enough that four of them fit in a prompt with room for the model to
# think, and large enough that a retrieved "First-Aid Measures" chunk
# contains the whole procedure rather than its first two steps. An
# SDS section written at normal length lands in this window on its own,
# which is the point of splitting on sections rather than on a fixed
# window.
MAX_TOKENS = 500
MIN_TOKENS = 120

# Tokens are estimated as words * 1.33 rather than counted. Counting
# exactly would mean adding tiktoken (wrong tokeniser for this model
# anyway) or loading the e5 tokeniser just to pick split points. The
# estimate only decides where to cut, and being 15% out moves a
# boundary by a sentence -- it does not change what a chunk is about.
TOKENS_PER_WORD = 1.33

# e5 was trained with asymmetric prefixes: "passage: " on stored
# documents, "query: " on the text you search with. This DIVERGES from
# language.py, which prefixes everything with "query: " -- correctly,
# because it compares text against language anchors, and both sides of
# that comparison play the same role. Retrieval is not symmetric: a
# two-word query and a 400-token SDS section are different kinds of
# object and the model was trained to encode them differently. Matching
# language.py here would mean deliberately using the model in a way it
# was not trained for. Do not "fix" this to match; the query side in
# the API must stay "query: " for the same reason.
PASSAGE_PREFIX = "passage: "


# ============================================================
# PARSING
# ============================================================

def estimate_tokens(text):
    """Rough token count. See TOKENS_PER_WORD for why this is an estimate."""

    return int(len(text.split()) * TOKENS_PER_WORD)


def parse_front_matter(raw):
    """
    Pull the leading `---` block off a corpus file.

    Hand-rolled rather than PyYAML: the block is a handful of
    `key: value` lines, and a whole YAML dependency to read four keys
    would be the largest thing in requirements.txt for the smallest
    reason. Returns (metadata_dict, remaining_body).
    """

    if not raw.startswith("---"):
        return {}, raw

    parts = raw.split("---", 2)

    if len(parts) < 3:
        return {}, raw

    metadata = {}

    for line in parts[1].strip().splitlines():

        if ":" not in line:
            continue

        key, _, value = line.partition(":")
        metadata[key.strip()] = value.strip()

    return metadata, parts[2]


def split_sections(body):
    """
    Split a document on its `## ` headings into (title, text) pairs.

    Everything before the first heading -- the title and the "this is a
    sample, not a real SDS" banner -- is deliberately dropped rather
    than indexed. It is document metadata, not content: it would answer
    no query about a substance, and with top-k of 4 a disclaimer chunk
    that did somehow rank would displace a chunk containing an actual
    first-aid step. The disclaimer stays in the source files, and the
    endpoint states it in the prompt instead.
    """

    sections = []
    current_title = None
    current_lines = []

    for line in body.splitlines():

        heading = re.match(r"^##\s+(.*\S)\s*$", line)

        if heading:

            if current_title is not None:
                sections.append((current_title, "\n".join(current_lines).strip()))

            current_title = heading.group(1)
            current_lines = []

        elif current_title is not None:
            current_lines.append(line)

    if current_title is not None:
        sections.append((current_title, "\n".join(current_lines).strip()))

    return [(title, text) for title, text in sections if text]


# ============================================================
# CHUNKING
# ============================================================

def split_oversized(text):
    """
    Split a section that is longer than MAX_TOKENS at paragraph
    boundaries, packing greedily.

    Only reached for a section that genuinely does not fit. The
    paragraph is the smallest unit this will cut at -- a mid-sentence
    or mid-paragraph cut is what produces the retrieved fragment that
    ends "do not apply water to" and drops the rest.
    """

    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]

    parts = []
    current = []

    for paragraph in paragraphs:

        candidate = current + [paragraph]

        if current and estimate_tokens("\n\n".join(candidate)) > MAX_TOKENS:
            parts.append("\n\n".join(current))
            current = [paragraph]

        else:
            current = candidate

    if current:
        parts.append("\n\n".join(current))

    return parts


def chunk_document(path, metadata, sections):
    """
    Turn one parsed document into chunk records.

    Three rules, in order:

      1. One chunk per section. Section boundaries are real semantic
         boundaries in both an SDS and a regulation -- "First-Aid
         Measures" is a complete answer to a first-aid question -- so
         they are the default cut points.
      2. A section over MAX_TOKENS is split at paragraph boundaries,
         carrying its title onto every part so provenance survives.
      3. A section under MIN_TOKENS is merged into the previous chunk
         of the same document when the two fit together. A 60-token
         chunk retrieved on its own rarely carries enough context to
         act on, and merging it with its neighbour costs nothing.

    Cross-document merging never happens -- rule 3 is scoped to one
    file, so a short section at the end of the chlorine sheet cannot
    be glued to the start of the ammonia one.
    """

    source_file = os.path.basename(path)

    doc_type = metadata.get("doc_type") or "unknown"

    # Regulations apply across substances, so they carry no substance
    # code. Null rather than "" or "N/A": the API filters and displays
    # on this field, and an empty string is a value that has to be
    # special-cased at every call site.
    substance_code = metadata.get("substance_code") or None

    chunks = []

    for title, text in sections:

        for part_index, part in enumerate(split_oversized(text)):

            titled = "%s\n\n%s" % (title, part)

            previous = chunks[-1] if chunks else None

            mergeable = (
                previous is not None
                and estimate_tokens(part) < MIN_TOKENS
                and estimate_tokens(previous["text"] + "\n\n" + titled) <= MAX_TOKENS
            )

            if mergeable:
                previous["text"] += "\n\n" + titled
                previous["section_title"] += "; " + title
                previous["token_estimate"] = estimate_tokens(previous["text"])
                continue

            chunks.append({
                "chunk_id": "%s#%02d" % (source_file, len(chunks)),
                "source_file": source_file,
                "substance_code": substance_code,
                "section_title": title,
                "doc_type": doc_type,
                "text": titled,
                "token_estimate": estimate_tokens(titled),
            })

    return chunks


# ============================================================
# CORPUS
# ============================================================

def load_corpus(corpus_dir):
    """
    Read every .md file in the corpus directory, in sorted order.

    Sorted so a rebuild on an unchanged corpus produces the same row
    ordering. Row ids leak into the metadata sidecar, and an index that
    reshuffles itself on every build makes two runs impossible to
    compare.
    """

    paths = sorted(
        os.path.join(corpus_dir, name)
        for name in os.listdir(corpus_dir)
        if name.endswith(".md")
    )

    if not paths:
        raise SystemExit("no .md files found in %s" % corpus_dir)

    chunks = []
    per_file = []

    for path in paths:

        with open(path, encoding="utf-8") as handle:
            raw = handle.read()

        metadata, body = parse_front_matter(raw)
        sections = split_sections(body)

        document_chunks = chunk_document(path, metadata, sections)

        chunks.extend(document_chunks)
        per_file.append((os.path.basename(path), len(sections), len(document_chunks)))

    return chunks, per_file, paths


def corpus_fingerprint(paths):
    """
    Hash of the corpus contents, used to decide whether a rebuild is
    needed. Covers filenames as well as bytes, so renaming a file
    counts as a change even if its content did not.
    """

    digest = hashlib.sha256()

    for path in paths:

        digest.update(os.path.basename(path).encode("utf-8"))

        with open(path, "rb") as handle:
            digest.update(handle.read())

    return digest.hexdigest()


# ============================================================
# BUILD
# ============================================================

def write_atomically(output_dir, index, records, manifest):
    """
    Write the index and its sidecar, or leave both as they were.

    Written to temporary names and renamed into place, because the two
    files only mean anything together. A run interrupted between them
    would otherwise leave a new index beside a stale sidecar -- an
    index that answers every query with confidently mislabelled
    sources, which is worse than one that fails to load.
    """

    import faiss

    index_path = os.path.join(output_dir, INDEX_FILENAME)
    metadata_path = os.path.join(output_dir, METADATA_FILENAME)

    index_tmp = index_path + ".tmp"
    metadata_tmp = metadata_path + ".tmp"

    faiss.write_index(index, index_tmp)

    with open(metadata_tmp, "w", encoding="utf-8") as handle:
        json.dump(
            {"manifest": manifest, "chunks": records},
            handle,
            ensure_ascii=False,
            indent=2,
        )

    os.replace(index_tmp, index_path)
    os.replace(metadata_tmp, metadata_path)


def build(corpus_dir, output_dir, force):

    if not os.path.isdir(corpus_dir):
        raise SystemExit("corpus directory not found: %s" % corpus_dir)

    chunks, per_file, paths = load_corpus(corpus_dir)

    fingerprint = corpus_fingerprint(paths)

    index_path = os.path.join(output_dir, INDEX_FILENAME)
    metadata_path = os.path.join(output_dir, METADATA_FILENAME)

    # Idempotence. Rebuilding an unchanged corpus produces a
    # byte-different index for no reason and costs the model load, so
    # the default is to notice and stop. --force is the escape hatch
    # for "I changed the chunking rules, not the corpus".
    if not force and os.path.exists(index_path) and os.path.exists(metadata_path):

        try:
            with open(metadata_path, encoding="utf-8") as handle:
                existing = json.load(handle).get("manifest", {})

        except (ValueError, OSError):
            existing = {}

        if (
            existing.get("corpus_sha256") == fingerprint
            and existing.get("model") == language.MODEL_NAME
        ):
            print("corpus unchanged (%s), index is current -- nothing to do."
                  % fingerprint[:12])
            print("use --force to rebuild anyway.")
            return 0

    print("corpus: %s" % corpus_dir)

    for name, section_count, chunk_count in per_file:
        print("  %-36s %2d sections -> %2d chunks" % (name, section_count, chunk_count))

    sizes = sorted(chunk["token_estimate"] for chunk in chunks)

    print("\n%d chunks, estimated tokens: min %d, median %d, max %d"
          % (len(chunks), sizes[0], sizes[len(sizes) // 2], sizes[-1]))

    oversized = [c for c in chunks if c["token_estimate"] > MAX_TOKENS]

    if oversized:
        print("warning: %d chunk(s) over %d tokens (unsplittable paragraphs):"
              % (len(oversized), MAX_TOKENS))
        for chunk in oversized:
            print("  %s (%d)" % (chunk["chunk_id"], chunk["token_estimate"]))

    print("\nloading %s ..." % language.MODEL_NAME)

    started = time.time()

    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(language.MODEL_NAME)

    print("loaded in %.1fs" % (time.time() - started))

    embeddings = model.encode(
        [PASSAGE_PREFIX + chunk["text"] for chunk in chunks],
        normalize_embeddings=True,
        show_progress_bar=False,
    )

    import faiss
    import numpy

    vectors = numpy.asarray(embeddings, dtype="float32")

    dimension = int(vectors.shape[1])

    # Inner product over normalised vectors is cosine similarity.
    # IndexFlatIP is exact and needs no training -- at this corpus size
    # an approximate index would add tuning parameters and recall
    # questions to save microseconds nobody would notice.
    index = faiss.IndexFlatIP(dimension)
    index.add(vectors)

    os.makedirs(output_dir, exist_ok=True)

    manifest = {
        "model": language.MODEL_NAME,
        "passage_prefix": PASSAGE_PREFIX,
        "dimension": dimension,
        "chunk_count": len(chunks),
        "corpus_sha256": fingerprint,
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "corpus_note": (
            "Illustrative sample corpus drafted for development. Not real "
            "SDS documents and not real regulatory text. Must be replaced "
            "before any real deployment."
        ),
    }

    write_atomically(output_dir, index, chunks, manifest)

    print("\nwrote %s (%d vectors, dim %d)" % (index_path, index.ntotal, dimension))
    print("wrote %s" % metadata_path)

    return 0


def main():

    parser = argparse.ArgumentParser(
        description="Build the FAISS index for POST /incident.",
    )

    parser.add_argument("--corpus", default=CORPUS_DIR)
    parser.add_argument("--out", default=OUTPUT_DIR)
    parser.add_argument(
        "--force",
        action="store_true",
        help="rebuild even if the corpus is unchanged",
    )

    args = parser.parse_args()

    return build(args.corpus, args.out, args.force)


if __name__ == "__main__":
    sys.exit(main())
