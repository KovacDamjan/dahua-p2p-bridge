#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef long long LLONG;
typedef unsigned long DWORD;
typedef int BOOL;
typedef BOOL (__cdecl *PFN_INIT)(void);
typedef void (__cdecl *PFN_CLEANUP)(void);
typedef BOOL (__cdecl *PFN_SET_CB)(void *callback, unsigned long long user);
typedef LLONG (__cdecl *PFN_LOGIN)(const char *ip, unsigned short port,
    const char *user, const char *password, void *device_info, int *error);
typedef BOOL (__cdecl *PFN_START_LISTEN)(LLONG login_id);
typedef BOOL (__cdecl *PFN_STOP_LISTEN)(LLONG login_id);
typedef BOOL (__cdecl *PFN_LOGOUT)(LLONG login_id);

static volatile int running = 1;

static BOOL __cdecl alarm_callback(long command, LLONG login_id, char *buf,
    DWORD len, char *ip, long port, unsigned long long user) {
    (void)login_id; (void)user;
    printf("EVENT command=0x%08lx length=%lu source=%s:%ld\\n",
           (unsigned long)command, len, ip ? ip : "", port);
    if (buf && len) {
        printf("DATA ");
        for (DWORD i = 0; i < len && i < 256; ++i)
            printf("%02X", (unsigned char)buf[i]);
        printf("\\n");
    }
    fflush(stdout);
    return 1;
}

static BOOL WINAPI console_handler(DWORD type) {
    (void)type; running = 0; return TRUE;
}

int main(int argc, char **argv) {
    const char *ip = argc > 1 ? argv[1] : "127.0.0.1";
    unsigned short port = (unsigned short)(argc > 2 ? atoi(argv[2]) : 18080);
    const char *user = argc > 3 ? argv[3] : "admin";
    const char *password = argc > 4 ? argv[4] : "";

    SetConsoleCtrlHandler(console_handler, TRUE);
    HMODULE sdk = LoadLibraryA("dhnetsdk.dll");
    if (!sdk) { fprintf(stderr, "LoadLibrary dhnetsdk.dll failed: %lu\\n", GetLastError()); return 2; }

    PFN_INIT init = (PFN_INIT)GetProcAddress(sdk, "CLIENT_Init");
    PFN_CLEANUP cleanup = (PFN_CLEANUP)GetProcAddress(sdk, "CLIENT_Cleanup");
    PFN_SET_CB set_cb = (PFN_SET_CB)GetProcAddress(sdk, "CLIENT_SetDVRMessCallBack");
    PFN_LOGIN login = (PFN_LOGIN)GetProcAddress(sdk, "CLIENT_Login");
    PFN_START_LISTEN start = (PFN_START_LISTEN)GetProcAddress(sdk, "CLIENT_StartListenEx");
    PFN_STOP_LISTEN stop = (PFN_STOP_LISTEN)GetProcAddress(sdk, "CLIENT_StopListen");
    PFN_LOGOUT logout = (PFN_LOGOUT)GetProcAddress(sdk, "CLIENT_Logout");

    if (!init || !cleanup || !set_cb || !login || !start || !stop || !logout) {
        fprintf(stderr, "Required dhnetsdk exports are missing\\n");
        FreeLibrary(sdk); return 3;
    }

    if (!init()) { fprintf(stderr, "CLIENT_Init failed\\n"); FreeLibrary(sdk); return 4; }
    set_cb((void *)alarm_callback, 0);

    unsigned char device_info[512] = {0};
    int error = 0;
    LLONG id = login(ip, port, user, password, device_info, &error);
    printf("LOGIN id=%lld error=%d local=%s:%u\\n", id, error, ip, port);
    if (!id) { cleanup(); FreeLibrary(sdk); return 5; }

    if (!start(id)) {
        fprintf(stderr, "CLIENT_StartListenEx failed\\n");
        logout(id); cleanup(); FreeLibrary(sdk); return 6;
    }

    printf("LISTENING for alarm events; press Ctrl+C to stop\\n");
    while (running) Sleep(500);
    stop(id);
    logout(id);
    cleanup();
    FreeLibrary(sdk);
    return 0;
}
