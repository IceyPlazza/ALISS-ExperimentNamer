"""Connectivity check for the Box integration.

Run after filling in the BOX_* variables in .env (from the repo root, so
`core` is importable):

    .venv\\Scripts\\python.exe -m core.box.box_setup_check

Verifies auth, then confirms both experiment directories are reachable and
shows a sample of what's inside each.
"""

from dotenv import load_dotenv

load_dotenv()

from core.box import box_client
from core.box.box_client import BOX_DIRECTORIES, BoxNotConfiguredError


def main():
    try:
        client = box_client.get_client()
    except BoxNotConfiguredError as e:
        print(f"NOT CONFIGURED: {e}")
        print("Fill in the BOX_* variables in .env (see .env.example).")
        return 1

    me = client.users.get_user_me()
    print(f"Connected to Box as: {me.name} <{me.login}>")

    ok = True
    for dir_key in BOX_DIRECTORIES:
        print(f"\n[{dir_key}]")
        try:
            folder_id = box_client._directory_folder_id(dir_key)
            info = box_client.directory_info(dir_key)
            if info["path"] is None:
                raise RuntimeError(
                    f"could not resolve folder id {folder_id} — check the id "
                    "and your access to it"
                )
            print(f"  Folder id {folder_id} -> \"{info['label']}\"")
            print(f"  Full path: {info['path']}")
            items = list(box_client._iter_subfolders(dir_key))
            print(f"  {len(items)} subfolder(s). First few:")
            for item in items[:5]:
                print(f"    - {item.name}")
        except BoxNotConfiguredError as e:
            print(f"  NOT CONFIGURED: {e}")
            ok = False
        except Exception as e:
            print(f"  ERROR: {e}")
            ok = False

    print("\nAll good — Box is connected." if ok else "\nFix the issues above and re-run.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
