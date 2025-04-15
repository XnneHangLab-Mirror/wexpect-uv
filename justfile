VERSION := `uv run scripts/get-version.py src/wexpect/__version__.py`

build:
  uv build

clean-builds:
  rm -rf dist/

release:
  @echo 'Tagging v{{VERSION}}...'
  git tag "v{{VERSION}}"
  @echo 'Push to GitHub to trigger publish process...'
  git push --tags

fmt-docs:
  prettier --ignore-path .prettierignore --write '**/*.md'