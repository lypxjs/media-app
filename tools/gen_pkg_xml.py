"""生成符合 AGP schema 的 package.xml（简化版）。

踩坑记录（按时间顺序，均为实测）
--------------------------------
1. 腾讯云镜像的组件 zip **不含 package.xml**，必须手写补齐。
2. `sdk.dir` 写转义反斜杠（`D\:\\as\\Sdk`）→ AGP 的
   SdkLocator.validateSdkPath 抛 `IOException: Invalid file path`。
   正斜杠（`C:/Users/...`）才通过。
3. `<channel>` 内放`<display-name>` → JAXB 反序列化报错
   "channel 的子元素应为 archives/display-name/... "。
4. `<display-name>` 必须是 remotePackage/localPackage 的**直接子元素**，
   不能只放在 channel 里，否则 `LocalPackage.getDisplayName()` 返回 null。
5. `<type-details xsi:type="ns4:platformDetailsType">` →
   `UnmarshalException: Unable to create an instance of TypeDetails`。
   AGP 8 对 platforms 包要求更严，故**去掉 type-details**，
   api-level 改用 `AndroidVersion.ApiLevel`（source.properties 里已有）+ 省略该节点。

最终结构：仅 localPackage / remotePackage + revision + display-name。
"""
import os

SDK = r"C:\android-sdk"

HEADER = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<ns2:repository xmlns:ns2="http://schemas.android.com/repository/android/common/02"
                xmlns:ns3="http://schemas.android.com/repository/android/generic/02">
'''

REPO_URL = "https://dl.google.com/android/repository/repository2-3.xml"


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def write_pkg(rel_dir, pkg_path, display_name, major=1, minor=None):
    path = os.path.join(SDK, rel_dir.replace("/", os.sep), "package.xml")
    os.makedirs(os.path.dirname(path), exist_ok=True)

    rev = f'        <revision><major>{major}</major>'
    if minor is not None:
        rev += f"<minor>{minor}</minor>"
    rev += "</revision>\n"

    body = HEADER
    body += f'    <localPackage path="{esc(pkg_path)}" obsolete="false">\n'
    body += rev
    body += f"        <display-name>{esc(display_name)}</display-name>\n"
    body += "    </localPackage>\n"
    body += f'    <remotePackage path="{esc(pkg_path)}" obsolete="false">\n'
    body += f'        <channel id="channel_0" url="{REPO_URL}"/>\n'
    body += rev
    body += f"        <display-name>{esc(display_name)}</display-name>\n"
    body += "    </remotePackage>\n"
    body += "</ns2:repository>\n"

    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(body)
    print(f"  {rel_dir}/package.xml  ({pkg_path})")


if __name__ == "__main__":
    write_pkg("platform-tools", "platform-tools",
              "Android SDK Platform-Tools", 37, 0)
    write_pkg("build-tools/34.0.0", "build-tools;34.0.0",
              "Android SDK Build-Tools 34", 34, 0)
    write_pkg("platforms/android-35", "platforms;android-35",
              "Android SDK Platform 35", 2)
    print("\npackage.xml 已重写（简化结构）")
