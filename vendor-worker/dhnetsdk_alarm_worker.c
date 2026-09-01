#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdio.h>
#include <stdlib.h>

typedef LONGLONG LLONG;
typedef BOOL (WINAPI *PFN_INIT)(void);
typedef void (WINAPI *PFN_CLEANUP)(void);
typedef void (WINAPI *PFN_SET_CB)(void *callback, ULONGLONG user);
typedef LLONG (WINAPI *PFN_LOGINEX)(const char *ip, unsigned short port,
    const char *user, const char *password, int spec_cap, void *cap_param,
    void *device_info, int *error);
typedef BOOL (WINAPI *PFN_START_LISTEN)(LLONG login_id);
typedef BOOL (WINAPI *PFN_STOP_LISTEN)(LLONG login_id);
typedef BOOL (WINAPI *PFN_LOGOUT)(LLONG login_id);

static volatile LONG running = 1;

static BOOL CALLBACK alarm_callback(long command, LLONG login_id, char *buf,
    DWORD len, char *ip, long port, ULONGLONG user) {
    (void)login_id; (void)user;
    printf("EVENT command=0x%08lx length=%lu source=%s:%ld\n",
           (unsigned long)command, (unsigned long)len, ip ? ip : "", port);
    if (buf && len) {
        printf("DATA ");
        for (DWORD i = 0; i < len && i < 256; ++i)
            printf("%02X", (unsigned char)buf[i]);
        printf("\n");
    }
    fflush(stdout);
    return TRUE;
}

static BOOL WINAPI console_handler(DWORD type) {
    (void)type; InterlockedExchange(&running, 0); return TRUE;
}

int main(int argc, char **argv) {
    const char *ip = argc > 1 ? argv[1] : "127.0.0.1";
    unsigned short port = (unsigned short)(argc > 2 ? atoi(argv[2]) : 18080);
    const char *user = argc > 3 ? argv[3] : "admin";
    const char *password = argc > 4 ? argv[4] : "";

    SetConsoleCtrlHandler(console_handler, TRUE);
    HMODULE sdk = LoadLibraryA("dhnetsdk.dll");
    if (!sdk) { fprintf(stderr, "LoadLibrary dhnetsdk.dll failed: %lu\n", GetLastError()); return 2; }

    PFN_INIT init = (PFN_INIT)GetProcAddress(sdk, "CLIENT_Init");
    PFN_CLEANUP cleanup = (PFN_CLEANUP)GetProcAddress(sdk, "CLIENT_Cleanup");
    PFN_SET_CB set_cb = (PFN_SET_CB)GetProcAddress(sdk, "CLIENT_SetDVRMessCallBack");
    PFN_LOGINEX login = (PFN_LOGINEX)GetProcAddress(sdk, "CLIENT_LoginEx");
    PFN_START_LISTEN start = (PFN_START_LISTEN)GetProcAddress(sdk, "CLIENT_StartListenEx");
    PFN_STOP_LISTEN stop = (PFN_STOP_LISTEN)GetProcAddress(sdk, "CLIENT_StopListen");
    PFN_LOGOUT logout = (PFN_LOGOUT)GetProcAddress(sdk, "CLIENT_Logout");

    if (!init || !cleanup || !set_cb || !login || !start || !stop || !logout) {
        fprintf(stderr, "Required dhnetsdk exports are missing\n");
        FreeLibrary(sdk); return 3;
    }

    if (!init()) { fprintf(stderr, "CLIENT_Init failed\n"); FreeLibrary(sdk); return 4; }
    set_cb((void *)alarm_callback, 0);

    unsigned char device_info[4096] = {0};
    int error = 0;
    /* 19 = private penetrating/P2P login according to dhnetsdk.h. */
    LLONG id = login(ip, port, user, password, 19, NULL, device_info, &error);
    printf("LOGIN id=%lld error=%d local=%s:%u\n", id, error, ip, port);
    if (!id) { cleanup(); FreeLibrary(sdk); return 5; }

    if (!start(id)) {
        fprintf(stderr, "CLIENT_StartListenEx failed\n");
        logout(id); cleanup(); FreeLibrary(sdk); return 6;
    }

    printf("LISTENING for alarm events; press Ctrl+C to stop\n");
    while (InterlockedCompareExchange(&running, 1, 1)) Sleep(500);
    stop(id);
    logout(id);
    cleanup();
    FreeLibrary(sdk);
    return 0;
}
