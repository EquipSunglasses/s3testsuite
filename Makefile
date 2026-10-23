# s3tests build. The only release artifact is the self-extracting installer:
#   make            -> dist/s3tests-VERSION.run
# Needs only make, tar, gzip, sed, cksum and sha256sum (or shasum).

NAME     := s3tests
VERSION  := $(shell sed -n "s/^__version__ = '\(.*\)'/\1/p" lib/s3tests/__init__.py)
PYTHON   ?= python3
DIST     := dist
STAGE    := $(DIST)/stage/$(NAME)-$(VERSION)
PAYLOAD  := $(DIST)/$(NAME)-$(VERSION).tar.gz
RUN      := $(DIST)/$(NAME)-$(VERSION).run

SOURCES := $(shell find lib -name '*.py' ! -name '_build.py') bin/s3tests install.sh uninstall.sh \
           README.md LICENSE COVERAGE.html etc/s3tests.conf.sample $(wildcard examples/*.sh)

.PHONY: all run release verify-run print-sources check selftest coverage version clean help

all: run

help:
	@echo "make              build $(RUN)"
	@echo "make release      build and copy the .run to release/ with SHA256SUMS"
	@echo "make verify-run   check release/*.run is exactly this source tree"
	@echo "make check        byte-compile the library and run the offline self-test"
	@echo "make coverage     regenerate COVERAGE.html from the test code"
	@echo "make clean        remove dist/"

version:
	@echo $(VERSION)

run: $(RUN)

$(RUN): $(PAYLOAD) packaging/selfextract/build-run.sh
	packaging/selfextract/build-run.sh $(PAYLOAD)
	@rm -f $(PAYLOAD)

# The payload: the files install.sh needs, plus a build id (UTC date + checksum of the library).
$(PAYLOAD): $(SOURCES)
	rm -rf $(DIST)/stage && mkdir -p $(STAGE)
	tar cf - $(SOURCES) | (cd $(STAGE) && tar xf -)
	@build="$$(date -u +%Y%m%d)-$$(cat $$(find lib -name '*.py' ! -name '_build.py' | LC_ALL=C sort) | cksum | cut -d' ' -f1)"; \
	  printf '# written by make\nBUILD = %s\n' "'$$build'" > $(STAGE)/lib/s3tests/_build.py; \
	  echo "build $$build"
	(cd $(DIST)/stage && tar cf - $(NAME)-$(VERSION)) | gzip -9 > $@
	rm -rf $(DIST)/stage

# Copy the built installer to release/, the copy kept in git.
release: $(RUN)
	mkdir -p release
	cp $(RUN) release/
	cd release && { command -v sha256sum >/dev/null && sha256sum $(NAME)-*.run || shasum -a 256 $(NAME)-*.run; } > SHA256SUMS
	packaging/tools/verify-run.sh release/$(NAME)-$(VERSION).run

verify-run:
	packaging/tools/verify-run.sh

print-sources:
	@echo $(SOURCES)

selftest:
	S3TESTS_HOME=lib bin/s3tests --selftest

check:
	$(PYTHON) -c "import compileall,sys; sys.exit(0 if compileall.compile_dir('lib', quiet=1, legacy=False, force=True) else 1)"
	find lib -name __pycache__ -type d -prune -exec rm -rf {} +
	$(MAKE) selftest

coverage:
	$(PYTHON) -B packaging/tools/gen-coverage.py COVERAGE.html

clean:
	rm -rf $(DIST)
	find lib -name __pycache__ -type d -prune -exec rm -rf {} +
