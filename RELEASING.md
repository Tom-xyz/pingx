# Release process

## Prerequisites

```bash
# One-time setup — create a clean virtualenv for twine
python3 -m venv /tmp/twine-env
/tmp/twine-env/bin/pip install twine
```

PyPI credentials are stored in Claude memory (`pypi_credentials.md`).  
Rotate the token at pypi.org → Account Settings → API Tokens after each use.

---

## Steps

### 1. Make and commit all changes

```bash
cd ~/Desktop/AI-Workspace-Projects/pingx
# ... edit files ...
python3 -m pytest tests/          # must be 100% green
git add -p
git commit -m "Description of changes"
git push
```

### 2. Bump version

Update the version string in **two places**:

```bash
# pingx.py
__version__ = "X.Y.Z"

# pyproject.toml
version = "X.Y.Z"
```

Commit:

```bash
git add pingx.py pyproject.toml
git commit -m "Bump version to vX.Y.Z"
git push
```

### 3. Tag the release

```bash
git tag vX.Y.Z
git push origin vX.Y.Z
```

### 4. Build

```bash
rm -rf dist/ build/ pingx.egg-info/
python3 -m build
# Verify pingx.py is in the wheel:
python3 -c "import zipfile; print('\n'.join(zipfile.ZipFile('dist/pingx-X.Y.Z-py3-none-any.whl').namelist()))"
```

### 5. Publish to PyPI

```bash
TWINE_USERNAME="__token__" \
TWINE_PASSWORD="<api-token>" \
/tmp/twine-env/bin/twine upload dist/*
```

### 6. Update Homebrew tap

```bash
# Get SHA256 of the new release tarball
SHA=$(curl -sL https://github.com/Tom-xyz/pingx/archive/refs/tags/vX.Y.Z.tar.gz | shasum -a 256 | awk '{print $1}')

# Update Formula/pingx.rb in the tap repo
cd /tmp/homebrew-tap   # or wherever the tap is cloned
git pull
# Edit Formula/pingx.rb: update url (tag) and sha256
git add Formula/pingx.rb
git commit -m "Update pingx to vX.Y.Z"
git push
```

The tap repo is at: https://github.com/Tom-xyz/homebrew-tap

### 7. Sync local binary

```bash
cp ~/Desktop/AI-Workspace-Projects/pingx/pingx.py /usr/local/bin/pingx
```

### 8. Create GitHub release

```bash
gh release create vX.Y.Z --repo Tom-xyz/pingx \
  --title "vX.Y.Z" \
  --notes "Release notes here"
```

---

## Quick checklist

- [ ] All tests pass (`python3 -m pytest tests/`)
- [ ] Version bumped in `pingx.py` and `pyproject.toml`
- [ ] Committed and pushed
- [ ] Git tag pushed
- [ ] `dist/` built and wheel contains `pingx.py`
- [ ] Uploaded to PyPI
- [ ] Homebrew tap formula updated with new tag + SHA256
- [ ] Local binary synced (`/usr/local/bin/pingx`)
- [ ] GitHub release created
