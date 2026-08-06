# Publishing `acel-core` to PyPI

The package is built and validated. These are the steps only you can do,
since they need your own PyPI account and credentials.

## 1. Create accounts

- Create an account at https://pypi.org/account/register/
- (Recommended) also create one at https://test.pypi.org/account/register/
  so you can do a dry-run upload first — it's a completely separate site/login.
- Enable 2FA on PyPI (it's required for new accounts now).

## 2. Create an API token

Don't use your PyPI password directly. Instead:

1. Go to https://pypi.org/manage/account/token/
2. Click "Add API token"
3. Scope: for the first upload it must be account-wide ("Entire account"),
   since the project doesn't exist on PyPI yet. After this first upload you
   can create a project-scoped token instead and delete the account-wide one.
4. Copy the token — it starts with `pypi-` and is only shown once.

## 3. Install twine

```powershell
pip install twine
```

## 4. (Recommended) Test upload first

```powershell
cd C:\Users\kanda\OneDrive\Desktop\Acel\acel-core
python -m twine upload --repository testpypi dist/*
```

- Username: `__token__`
- Password: the TestPyPI token (from https://test.pypi.org/manage/account/token/,
  a separate token from the real one)

Then verify it installed cleanly in a scratch venv:

```powershell
python -m venv test_install
test_install\Scripts\activate
pip install --index-url https://test.pypi.org/simple/ --no-deps acel-core
python -c "import acel; print(acel.__version__)"
deactivate
```

## 5. Real upload

```powershell
cd C:\Users\kanda\OneDrive\Desktop\Acel\acel-core
python -m twine upload dist/*
```

- Username: `__token__`
- Password: your real PyPI token from step 2

## 6. Verify

```powershell
pip install acel-core
```

and check the project page at `https://pypi.org/project/acel-core/`.

## Already done for you

- `dist/acel_core-0.1.0.tar.gz` and `dist/acel_core-0.1.0-py3-none-any.whl`
  are built and sitting in this folder.
- Both passed `twine check` (validates metadata, README rendering, etc.) —
  PyPI's own upload validator runs the same check, so this upload should not
  be rejected for metadata reasons.
- `pyproject.toml` has real author info, keywords, classifiers, and project
  URLs pointing at the GitHub repo.

## Re-publishing a new version later

PyPI does not allow re-uploading the same version number, even if you delete
it. For any future change:

1. Bump `version` in `pyproject.toml` **and** `__version__` in
   `src/acel/__init__.py` (keep them in sync).
2. Delete the old `dist/` folder contents.
3. Rebuild: `python -m build`
4. `twine check dist/*`
5. `twine upload dist/*`
