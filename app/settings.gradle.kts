// 仓库配置：本机网络下 Google / Maven Central 均不可达
// （dl.google.com 000、repo1.maven.org 000、maven.aliyun.com 000），
// 全部改走腾讯云镜像（已实测全部 200）。
//
// 注意：Gradle 8 的 Kotlin DSL 中 maven{} 的 url 是 Property<String>，
// 必须用 `setUrl()` 或单引号简写（url = "..."），旧式 url '...' 已废弃。
pluginManagement {
    repositories {
        maven { setUrl("https://mirrors.cloud.tencent.com/nexus/repository/maven-public/") }
        maven { setUrl("https://mirrors.cloud.tencent.com/gradle/") }
        gradlePluginPortal()
        google()
        mavenCentral()
    }
}

dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.PREFER_SETTINGS)
    repositories {
        maven { setUrl("https://mirrors.cloud.tencent.com/nexus/repository/maven-public/") }
        google()
        mavenCentral()
    }
}

rootProject.name = "MediaDB"
include(":app")
