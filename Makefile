#
# Author: Alec S. Adair
# ROAR - Turku, Finland
#
# Setup uses Pixi. Environment-file generation remains an optional legacy aid.

SHELL := /bin/sh
OUTPUT_FILE := roar_env.csh
OUTPUT_FILE_SH := roar_env.sh

.PHONY: all install generate_roar_env generate_roar_env_bash clean distclean

all: generate_roar_env

install:
	./bin/setup_roar.sh
	@printf '%s\n' 'Launch ROAR with ./bin/roar; shell activation is not required.'

generate_roar_env:
	@root=$$(pwd | sed 's/[\\"$$`!]/\\&/g'); \
	{ \
		printf '%s\n' '# Optional ROAR environment for csh/tcsh; bin/roar does not require it.'; \
		printf 'setenv ROAR_HOME "%s"\n' "$$root"; \
		printf '%s\n' \
			'setenv ROAR_SRC "$$ROAR_HOME/src"' \
			'setenv ROAR_DESIGN "$$ROAR_HOME/design"' \
			'setenv ROAR_LIB "$$ROAR_HOME/lib"' \
			'setenv ROAR_DEPENDENCIES "$$ROAR_HOME/dependencies"' \
			'setenv ROAR_CHARACTERIZATION "$$ROAR_HOME/characterization"'; \
	} > "$(OUTPUT_FILE)"
	@printf 'Generated optional environment file: %s\n' "$(OUTPUT_FILE)"

generate_roar_env_bash:
	@root=$$(pwd | sed 's/[\\"$$`]/\\&/g'); \
	{ \
		printf '%s\n' '# Optional ROAR environment for sh/bash; bin/roar does not require it.'; \
		printf 'export ROAR_HOME="%s"\n' "$$root"; \
		printf '%s\n' \
			'export ROAR_SRC="$$ROAR_HOME/src"' \
			'export ROAR_DESIGN="$$ROAR_HOME/design"' \
			'export ROAR_LIB="$$ROAR_HOME/lib"' \
			'export ROAR_DEPENDENCIES="$$ROAR_HOME/dependencies"' \
			'export ROAR_CHARACTERIZATION="$$ROAR_HOME/characterization"'; \
	} > "$(OUTPUT_FILE_SH)"
	@printf 'Generated optional environment file: %s\n' "$(OUTPUT_FILE_SH)"

clean:
	@rm -f -- "$(OUTPUT_FILE)" "$(OUTPUT_FILE_SH)"
	@printf '%s\n' 'Removed generated environment files.'

# Legacy release artifacts only; managed Pixi environments are preserved.
distclean: clean
	rm -rf .venv .appimage-build bin/ROAR-*.AppImage
	@printf '%s\n' 'Removed legacy .venv and AppImage build artifacts.'
