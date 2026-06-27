plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.jarvis.companion"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.jarvis.companion"
        minSdk = 28              // TelecomManager.endCall() is API 28+
        // targetSdk 33 on purpose: Android 14 (API 34) refuses to START a `phoneCall` foreground
        // service unless the app is the default dialer/has an active call — which would crash this
        // monitor on boot. Targeting 33 keeps the lenient pre-34 FGS behaviour; the app still runs
        // fine on Android 14 devices.
        targetSdk = 33
        versionCode = 1
        versionName = "1.0"
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
}
