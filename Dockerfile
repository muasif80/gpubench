# gpubench verification container.
#
#   docker build -t gpubench-verify --build-arg GPUBENCH_COMMIT=$(git rev-parse HEAD) .
#   docker run --rm gpubench-verify tools/verify_claims.py --selftest
#   docker run --rm -v /path/to/report:/work gpubench-verify \
#          tools/verify_claims.py /work/article/public/claims.json --root /work
#   docker run --rm -v /path/to/out:/work gpubench-verify \
#          tools/reproduce.py --out-dir /work/run --explain
#
# WHAT THIS CONTAINER IS FOR, AND WHAT IT DELIBERATELY IS NOT
# ----------------------------------------------------------
# It runs the two verification tools: tools/verify_claims.py, which rebuilds a report's derived
# claims from the raw result artefacts, and tools/reproduce.py, which drives the harness and
# writes a pinned, hashed reproduction record. Both are pure standard library, and so are the
# seven test suites, which is why this image needs nothing beyond an interpreter and an ssh
# client. There is no pip install line anywhere below, and there should never be one.
#
# IT IS NOT A MEASUREMENT ENVIRONMENT, AND MUST NEVER BECOME ONE. gpubench installs nothing on the
# machine it measures: that is the design rule that lets it run against a production box, and it
# is also why this image must not carry a CUDA runtime or a PyTorch build of its own. If it did,
# a reader could not tell whether a number came from the toolchain the operator actually runs or
# from one this Dockerfile silently introduced. The torch and CUDA versions in a result file are
# the TARGET's. reproduce.py records them and does not pin them. The GPU driver is further out
# still: it belongs to the target host's kernel, no container can pin it from the outside, and
# reproduce.py lists it under not_pinned with exactly that reason.
#
# WHAT IS PINNED HERE
#   * The base image, BY DIGEST. A tag is a moving pointer: python:3.11-slim-bookworm names a
#     different set of bytes every few weeks, so an image pinned to a tag is not pinned at all and
#     a reproduction built from one reproduces nothing. The digest below is the multi-architecture
#     index digest, so it still resolves to the right platform while naming exact content. It is
#     declared once, before FROM, and reused for the label and the provenance file so the three
#     cannot drift apart. Refresh it deliberately with
#         docker buildx imagetools inspect python:3.11-slim-bookworm
#     and record the change, rather than letting a tag move underneath a published result.
#   * The benchmark source, by the SHA-256 reproduce.py computes over every shipped file. It goes
#     into /opt/gpubench/container-provenance.json at build time, computed from the code that is
#     actually in the image rather than from a build argument, so an image can be matched to its
#     source without trusting a label.
#
# WHAT IS ONLY RECORDED, NOT PINNED
#   * The apt package set. openssh-client is installed because reproduce.py drives an ssh://
#     target, and Debian's archive serves whatever version is current for the base image's suite.
#     The exact installed version is written into the provenance file so two images can be
#     compared. Pinning it properly needs a snapshot.debian.org source list, which is a decision
#     about the release process and not one this file should make on its own.
#   * The git commit. There is no .git directory in the image, on purpose. Pass the commit with
#     --build-arg GPUBENCH_COMMIT and it is recorded; leave it out and the provenance file says
#     "not-supplied" rather than implying a pin that is not there.

ARG BASE_DIGEST=sha256:0bee7276f83efd4a1ee05bbbf4281d95ed28e079220a9457f25a93e3f1e3c31b
FROM python:3.11-slim-bookworm@${BASE_DIGEST}

# Re-declared so the value is readable after FROM. Same argument, one definition.
ARG BASE_DIGEST
ARG GPUBENCH_COMMIT=not-supplied

LABEL org.opencontainers.image.title="gpubench-verify" \
      org.opencontainers.image.description="Rebuilds a report's derived claims from raw result artefacts, and runs the harness with a pinned reproduction record. No CUDA, no PyTorch, no GPU." \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.base.name="python:3.11-slim-bookworm" \
      org.opencontainers.image.base.digest="${BASE_DIGEST}" \
      org.opencontainers.image.revision="${GPUBENCH_COMMIT}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/opt/gpubench

WORKDIR /opt/gpubench

# openssh-client is the one thing reproduce.py needs that the interpreter does not provide, and
# only for an ssh:// target. Nothing else is installed. The version that landed is recorded below.
RUN apt-get update \
 && apt-get install --no-install-recommends -y openssh-client \
 && rm -rf /var/lib/apt/lists/*

# Copied by allow-list, mirroring what the release archives ship, with two deliberate differences.
#   results/ and reports/ are NOT here. Result files carry the target host, the board model and
#   device serials, and an image shipping one estate's numbers invites them to be quoted as the
#   tool's own. Mount them at /work instead.
#   tools/ is copied FILE BY FILE. tools/make_dist.py carries the site-specific deny literals that
#   the redaction mechanism exists to keep out of a release, so the builder must not travel inside
#   a shareable image any more than it travels inside a tarball.
COPY pyproject.toml LICENSE NOTICE README.md ./
COPY gpubench/ ./gpubench/
COPY tests/ ./tests/
COPY references/ ./references/
COPY examples/ ./examples/
COPY tools/verify_claims.py tools/reproduce.py ./tools/

# Build-time provenance, computed from the image's own contents.
RUN python -c "import json, os, subprocess, sys; \
sys.path.insert(0, '/opt/gpubench/tools'); \
import reproduce; \
digest, count = reproduce.source_digest(); \
out = subprocess.run(['dpkg', '-s', 'openssh-client'], stdout=subprocess.PIPE).stdout.decode('utf-8', 'replace'); \
ssh = ([l.split(':', 1)[1].strip() for l in out.splitlines() if l.startswith('Version:')] or ['not recorded'])[0]; \
json.dump({'schema': 'container-provenance/1', \
 'pinned': {'base_image': 'python:3.11-slim-bookworm', \
            'base_digest': os.environ.get('BASE_DIGEST', 'see the FROM line'), \
            'gpubench_source_sha256': digest, \
            'gpubench_source_files_hashed': count, \
            'python': sys.version.split()[0]}, \
 'recorded_not_pinned': {'openssh_client_version': ssh, \
   'why': 'Debian package versions follow the archive for the base image suite. Recorded so two images can be compared, not pinned.'}, \
 'git_commit': os.environ.get('GPUBENCH_COMMIT', 'not-supplied'), \
 'never_installed': ['cuda', 'torch', 'nvidia driver', 'pytest'], \
 'why_never_installed': 'the toolchain and the driver behind a measurement belong to the target host. An image carrying its own would make a result untraceable to the machine it came from.'}, \
 open('/opt/gpubench/container-provenance.json', 'w'), indent=2, sort_keys=True)" \
 && cat /opt/gpubench/container-provenance.json

# The image fails to build if its own checks fail. A container whose checks are green only when
# somebody remembers to run them is a container nobody runs checks in.
#
# WHAT IS CHECKED HERE IS THE IMAGE, NOT THE PROJECT. The seven test suites belong to CI
# (.github/workflows/verify.yml), which runs each on its own runner, and duplicating them in a
# build layer would mean a slow build that still proves nothing CI does not already prove. What
# this layer proves is the thing only the image can be wrong about: that the two tools, and the
# package they import, actually work inside these exact bytes.
#
# One of those suites additionally CANNOT run in a zero-dependency image as things stand.
# tests/test_gate.py has a TestDocumentTitleExtraction class that imports
# gpubench.longform.docx_export, which imports python-docx at module scope with no guard. Five of
# its tests therefore need a package this image deliberately does not install. The suite already
# has the right pattern for this a hundred lines earlier, at HAVE_PYTHON_DOCX and HAVE_FITZ, so
# the fix is a decorator on that class rather than a pip install here. Until it lands, the honest
# state is written down rather than worked around: a build that installed python-docx to make a
# suite pass would be an image lying about its own dependency surface.
RUN python tools/verify_claims.py --selftest \
 && python -m tests.test_template \
 && python -c "import gpubench.cli, gpubench.runner, gpubench.verify; print('package imports clean')" \
 && python -m gpubench --help > /dev/null \
 && python tools/reproduce.py --out-dir /tmp/explain --explain > /dev/null \
 && rm -rf /tmp/explain

# /work is where a report tree or an output directory gets mounted. Nothing is written into the
# image at run time. This is a mkdir and not a VOLUME: declaring a volume would silently create an
# anonymous one on every run that forgot the mount, which hides a missing bind rather than failing.
RUN mkdir -p /work

ENTRYPOINT ["python"]
CMD ["tools/verify_claims.py", "--selftest"]
