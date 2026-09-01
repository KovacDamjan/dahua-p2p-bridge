#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdio.h>

typedef void *(__cdecl *CREATE_ENTITY)(int);
typedef int (__cdecl *RELEASE_ENTITY)(void *);
typedef int (__cdecl *SET_PATH)(const char *);

static void probe_export(HMODULE sdk, const char *name) {
    FARPROC p = GetProcAddress(sdk, name);
    if (p) printf("EXPORT %-70s %p\\n", name, (void *)p);
}

int main(void) {
    HMODULE sdk = LoadLibraryA("PlatformSDK.dll");
    if (!sdk) {
        printf("LOAD_FAILED gle=%lu\\n", GetLastError());
        return 2;
    }

    SET_PATH set_config = (SET_PATH)GetProcAddress(sdk, "?SetConfigPath@DPSDKFactory@DPSdk@@SAHPEBD@Z");
    SET_PATH set_database = (SET_PATH)GetProcAddress(sdk, "?SetDataBasePath@DPSDKFactory@DPSdk@@SAHPEBD@Z");
    SET_PATH set_log = (SET_PATH)GetProcAddress(sdk, "?SetLogPath@DPSDKFactory@DPSdk@@SAHPEBD@Z");
    SET_PATH set_picture = (SET_PATH)GetProcAddress(sdk, "?SetPicturePath@DPSDKFactory@DPSdk@@SAHPEBD@Z");
    CREATE_ENTITY create_entity = (CREATE_ENTITY)GetProcAddress(
        sdk, "?CreateSDKEntity@DPSDKFactory@DPSdk@@SAPEAVIDPSDKEntity@2@_N@Z");
    RELEASE_ENTITY release_entity = (RELEASE_ENTITY)GetProcAddress(
        sdk, "?ReleaseSDKEntity@DPSDKFactory@DPSdk@@SAHPEAVIDPSDKEntity@2@@Z");

    printf("EXPORT CreateSDKEntity=%p ReleaseSDKEntity=%p\\n",
           (void *)create_entity, (void *)release_entity);
    if (set_config) printf("SetConfigPath rc=%d\\n", set_config("."));
    if (set_database) printf("SetDataBasePath rc=%d\\n", set_database("."));
    if (set_log) printf("SetLogPath rc=%d\\n", set_log("."));
    if (set_picture) printf("SetPicturePath rc=%d\\n", set_picture("."));

    /* Exported SDK entry points used by SmartPSS for login, streaming and events. */
    probe_export(sdk, "CLIENT_Login");
    probe_export(sdk, "CLIENT_LoginEx");
    probe_export(sdk, "CLIENT_LoginWithHighLevelSecurity");
    probe_export(sdk, "CLIENT_StartLoginEx");
    probe_export(sdk, "CLIENT_RealPlayEx");
    probe_export(sdk, "CLIENT_StopRealPlayEx");
    probe_export(sdk, "CLIENT_StartListenEx");
    probe_export(sdk, "CLIENT_StopListen");
    probe_export(sdk, "CLIENT_Subscribe");
    probe_export(sdk, "CLIENT_UnSubscribe");

    if (!create_entity) {
        FreeLibrary(sdk);
        return 3;
    }

    void *entity = create_entity(0);
    printf("ENTITY=%p\\n", entity);
    if (!entity) {
        FreeLibrary(sdk);
        return 4;
    }

    void **vtable = *(void ***)entity;
    printf("VTABLE=%p\\n", (void *)vtable);
    for (int i = 0; i < 64; ++i)
        printf("V[%02d]=%p\\n", i, vtable[i]);

    if (release_entity)
        printf("RELEASE rc=%d\\n", release_entity(entity));
    FreeLibrary(sdk);
    return 0;
}
