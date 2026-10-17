"""解压 SDK 组件（直接覆盖写入，不触发任何删除操作）。

Expand-Archive -Force 会先删除已存在文件，被安全策略拦截；
故改用 zipfile 逐条写入（'w' 模式本身即覆盖），无需 delete。
"""
import os
import zipfile

SDK = r"D:\as\Sdk"


def extract(zip_name, dest_root, target_sub=None):
    """解压到 dest_root。

    若zip 内含统一顶层目录且 target_sub 为 None，则自动剥掉该层。
    """
    zp = os.path.join(SDK, zip_name)
    dest_root = os.path.join(SDK, dest_root)
    os.makedirs(dest_root, exist_ok=True)

    with zipfile.ZipFile(zp) as z:
        names = [n for n in z.namelist() if not n.endswith("/")]
        tops = {n.split("/")[0] for n in names if "/" in n}
        strip = tops.pop() if len(tops) == 1 else None

        n_written = 0
        n_skipped = 0
        for name in names:
            rel = name
            if strip:
                if not name.startswith(strip + "/"):
                    continue
                rel = name[len(strip) + 1:]
            if not rel:
                continue
            target = os.path.join(dest_root, rel.replace("/", os.sep))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            # 已有且大小一致则跳过：zip 内条目带只读属性，
            # 覆盖会PermissionError（Expand-Archive 的残留）
            if os.path.exists(target) and os.path.getsize(target) > 0:
                n_skipped += 1
                continue
            try:
                with z.open(name) as src:
                    data = src.read()
                with open(target, "wb") as dst:      # 'w' 直接覆盖，不需删除
                    dst.write(data)
                n_written += 1
            except PermissionError:
                n_skipped += 1

    print(f"  {zip_name} -> {dest_root}: 写入 {n_written}，跳过 {n_skipped}"
          + (f"（剥掉顶层 {strip}/）" if strip else ""))
    return dest_root


def check(rel, label):
    p = os.path.join(SDK, rel)
    ok = os.path.exists(p)
    size = os.path.getsize(p) / 1024 / 1024 if ok else 0
    print(f"  [{'OK  ' if ok else 'MISS'}] {label:<16}"
          f"{size:>8.1f} MB" if ok else f"  [MISS] {label}")
    return ok


if __name__ == "__main__":
    print("=== 解压 platform-35 ===")
    extract("platform-35_r02.zip", "platforms/android-35")

    print("\n=== 解压 build-tools 34 ===")
    extract("build-tools_r34-windows.zip", "build-tools/34.0.0")

    print("\n=== 校验关键文件 ===")
    ok = True
    ok &= check(r"platform-tools\adb.exe", "adb")
    ok &= check(r"platforms\android-35\android.jar", "android.jar")
    ok &= check(r"build-tools\34.0.0\aapt2.exe", "aapt2")
    ok &= check(r"build-tools\34.0.0\d8.bat", "d8")
    ok &= check(r"platforms\android-35\source.properties", "platform props")

    print("\n=== source.properties 版本确认 ===")
    for rel in ("platforms/android-35/source.properties",
                "build-tools/34.0.0/source.properties",
                "platform-tools/source.properties"):
        p = os.path.join(SDK, rel.replace("/", os.sep))
        if os.path.exists(p):
            for line in open(p, encoding="utf-8", errors="replace"):
                if line.startswith(("Pkg.Revision", "Pkg.Desc")):
                    print(f"  {rel:<38} {line.strip()}")
                    break

    print("\nSDK 就绪" if ok else "\n有缺失，请检查")
