plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.android)
    alias(libs.plugins.kotlin.compose)
    alias(libs.plugins.ksp)
}

android {
    namespace = "com.partnerssolutions.caroline.companion"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.partnerssolutions.caroline.companion"
        // SEND_SMS/READ_SMS/READ_CONTACTS runtime permissions + the SMS content
        // provider are all available from 26 on; no reason to support lower.
        minSdk = 26
        targetSdk = 35
        // Set by build.bat (-PappVersion=<yyyyMMddHHmm>, -PappVersionCode=<minutes
        // since the epoch>) so every published build is a real, monotonically
        // increasing upgrade over the last one; plain ad-hoc gradlew runs fall
        // back to the skeleton defaults.
        versionCode = project.findProperty("appVersionCode")?.toString()?.toIntOrNull() ?: 1
        versionName = project.findProperty("appVersion")?.toString() ?: "0.1.0-skeleton"
        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
    }

    // Same signing key as Ratatosk/ShortNerdCat (d:\REPO\tf38key.jks, alias
    // tf38key); the password only ever comes from the environment, never the repo.
    signingConfigs {
        create("release") {
            storeFile = file(System.getenv("SNC_KEYSTORE") ?: "D:\\REPO\\tf38key.jks")
            storePassword = System.getenv("SNC_SIGN_PASSWORD") ?: ""
            keyAlias = "tf38key"
            keyPassword = System.getenv("SNC_SIGN_PASSWORD") ?: ""
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            signingConfig = signingConfigs.getByName("release")
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_11
        targetCompatibility = JavaVersion.VERSION_11
    }

    kotlinOptions {
        jvmTarget = "11"
    }

    buildFeatures {
        compose = true
    }
}

dependencies {
    implementation(libs.androidx.core.ktx)
    implementation(libs.androidx.lifecycle.runtime.ktx)
    implementation(libs.androidx.lifecycle.process)
    implementation(libs.androidx.lifecycle.viewmodel.ktx)
    implementation(libs.androidx.lifecycle.viewmodel.compose)
    implementation(libs.androidx.activity.compose)
    implementation(platform(libs.androidx.compose.bom))
    implementation(libs.androidx.ui)
    implementation(libs.androidx.ui.graphics)
    implementation(libs.androidx.ui.tooling.preview)
    implementation(libs.androidx.material3)
    implementation(libs.androidx.material.icons.extended)
    implementation(libs.androidx.navigation.compose)

    implementation(libs.androidx.room.runtime)
    implementation(libs.androidx.room.ktx)
    ksp(libs.androidx.room.compiler)

    implementation(libs.retrofit)
    implementation(libs.retrofit.converter.moshi)
    implementation(libs.okhttp.logging)
    implementation(libs.moshi.kotlin)
    ksp(libs.moshi.kotlin.codegen)

    implementation(libs.kotlinx.coroutines.android)

    implementation(libs.androidx.work.runtime.ktx)
    implementation(libs.coil.compose)
    implementation(libs.androidx.datastore.preferences)
    implementation(libs.androidx.security.crypto)
    implementation(libs.markwon.core)

    debugImplementation(libs.androidx.ui.tooling)
    debugImplementation(libs.androidx.ui.test.manifest)

    testImplementation(libs.junit)
    testImplementation(libs.kotlinx.coroutines.test)
    androidTestImplementation(libs.androidx.junit)
    androidTestImplementation(libs.androidx.espresso.core)
    androidTestImplementation(platform(libs.androidx.compose.bom))
    androidTestImplementation(libs.androidx.ui.test.junit4)
}
