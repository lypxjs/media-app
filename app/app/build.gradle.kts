plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    // library.db.gz 已是 gzip，APK 打包时再压一遍纯属浪费构建时间
    @Suppress("UnstableApiUsage")
    androidResources {
        noCompress += listOf("gz")
    }

    namespace = "com.mediadb.app"
    compileSdk = 35
    buildToolsVersion = "34.0.0"

    defaultConfig {
        applicationId = "com.mediadb.app"
        minSdk = 26           // Android 8.0，PiP 需要
        targetSdk = 35
        versionCode = 1
        versionName = "1.0"

        // 浏览页地址：内网优先，连不上自动回退到家里宽带的公网 IPv6
        buildConfigField("String", "WEB_BASE", "\"https://192.168.1.100:8443/?nosw=1\"")
        buildConfigField("String", "WEB_BASE_HTTP", "\"http://127.0.0.1:8091\"")
        buildConfigField("String", "WEB_BASE_LAN", "\"https://192.168.1.100:8443\"")
        buildConfigField(
            "String", "WEB_BASE_WAN",
            "\"https://[2001:db8::1]:8443\""
        )
        // 复用 LibreTV 播放器
        buildConfigField("String", "PLAYER_BASE", "\"https://192.168.1.100:8443\"")
    }

    buildTypes {
        release {
            // 自用：debug 签名即可安装，无需上架备案
            isMinifyEnabled = false
            signingConfig = signingConfigs.getByName("debug")
        }
        debug {
            isMinifyEnabled = false
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
    buildFeatures {
        buildConfig = true
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("androidx.webkit:webkit:1.11.0")
    implementation("androidx.constraintlayout:constraintlayout:2.1.4")
    // Media3（ExoPlayer）：原生 HLS 播放，替代 WebView 的 hls.js
    implementation("androidx.media3:media3-exoplayer:1.4.1")
    implementation("androidx.media3:media3-ui:1.4.1")
    implementation("androidx.media3:media3-exoplayer-hls:1.4.1")
    implementation("androidx.media3:media3-session:1.4.1")
    // 进度上报到 NAS
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
}
