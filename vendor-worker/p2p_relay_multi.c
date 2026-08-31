#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <signal.h>

typedef int (__cdecl *P2P_INIT)(int,const char*,int,const char*,const char*,void**,const char*);
typedef int (__cdecl *P2P_UNINIT)(int,void*);
typedef int (__cdecl *P2P_QUERY_SERVER_STATUS)(int,void*);
typedef int (__cdecl *P2P_DEVICE_STATUS)(int,void*,const char*);
typedef int (__cdecl *P2P_GET_DEVICE_INFO)(int,void*,const char*,int,char*);
typedef int (__cdecl *P2P_QUERY_CHANNEL_STATUS)(int,void*,int);
typedef int (__cdecl *P2P_CONNECT)(int,void*,const char*,int,int*,const char*,const char*,const char*,const char*);
typedef int (__cdecl *P2P_DISCONNECT)(int,void*,const char*,int,int);

typedef struct { int remote_port; int local_port; int connected; } PortMap;
static volatile int stop_requested = 0;
static void on_signal(int sig) { (void)sig; stop_requested = 1; }
static BOOL WINAPI on_console(DWORD type) { (void)type; stop_requested = 1; return TRUE; }

static const char *arg_value(int argc,char **argv,const char *name,const char *fallback) {
    for (int i=1;i+1<argc;i++) if (!strcmp(argv[i],name)) return argv[i+1];
    return fallback;
}
static int extract_json_string(const char *json,const char *key,char *out,size_t size) {
    const char *p=strstr(json,key); out[0]=0; if(!p) return 0;
    p=strchr(p,':'); if(!p) return 0; p++;
    while(*p==' '||*p=='\t'||*p=='\"') p++;
    const char *e=p; while(*e&&*e!='\"'&&*e!=','&&*e!='}'&&*e!='\r'&&*e!='\n') e++;
    size_t n=(size_t)(e-p); if(n>=size) n=size-1; memcpy(out,p,n); out[n]=0; return n>0;
}
static int parse_maps(int argc,char **argv,PortMap *maps,int max_maps) {
    int count=0;
    for(int i=1;i+1<argc&&count<max_maps;i++) if(!strcmp(argv[i],"--map")) {
        int remote=0,local=0;
        if(sscanf(argv[++i],"%d:%d",&remote,&local)==2&&remote>0&&local>0) {
            maps[count].remote_port=remote; maps[count].local_port=local; maps[count].connected=0; count++;
        }
    }
    return count;
}

int main(int argc,char **argv) {
    setvbuf(stdout,NULL,_IONBF,0); setvbuf(stderr,NULL,_IONBF,0);
    const char *serial=arg_value(argc,argv,"--serial",NULL);
    const char *user=arg_value(argc,argv,"--user",getenv("DAHUA_DEVICE_USER"));
    const char *password=arg_value(argc,argv,"--password",getenv("DAHUA_DEVICE_PASS"));
    const char *dll_dir=arg_value(argc,argv,"--dll-dir","Z:\\vendor");
    const char *server=arg_value(argc,argv,"--server","www.easy4ipcloud.com");
    PortMap maps[8]; int map_count=parse_maps(argc,argv,maps,8);
    if(!serial||!user||!password||map_count==0) { fprintf(stderr,"ERROR invalid arguments\n"); return 2; }
    signal(SIGINT,on_signal); signal(SIGTERM,on_signal); SetConsoleCtrlHandler(on_console,TRUE);
    SetDllDirectoryA(dll_dir); SetCurrentDirectoryA(dll_dir);
    HMODULE dll=LoadLibraryA("P2PDll.dll");
    if(!dll) { fprintf(stderr,"ERROR LoadLibrary P2PDll.dll gle=%lu\n",GetLastError()); return 3; }
#define LOAD(name) name=(void*)GetProcAddress(dll,#name)
    P2P_INIT P2P_Init; P2P_UNINIT P2P_UnInit; P2P_QUERY_SERVER_STATUS P2P_QueryServerStatus;
    P2P_DEVICE_STATUS P2P_DeviceStatus; P2P_GET_DEVICE_INFO P2P_GetDeviceInfo;
    P2P_QUERY_CHANNEL_STATUS P2P_QueryChannelStatus; P2P_CONNECT P2P_Connect; P2P_DISCONNECT P2P_Disconnect;
    LOAD(P2P_Init); LOAD(P2P_UnInit); LOAD(P2P_QueryServerStatus); LOAD(P2P_DeviceStatus);
    LOAD(P2P_GetDeviceInfo); LOAD(P2P_QueryChannelStatus); LOAD(P2P_Connect); LOAD(P2P_Disconnect);
    if(!P2P_Init||!P2P_UnInit||!P2P_QueryServerStatus||!P2P_DeviceStatus||!P2P_GetDeviceInfo||!P2P_QueryChannelStatus||!P2P_Connect||!P2P_Disconnect) {
        fprintf(stderr,"ERROR missing P2PDll exports\n"); FreeLibrary(dll); return 4;
    }
    void *handle=NULL; int type=0;
    void *map_handles[8]={0};
    int rc=P2P_Init(type,server,8800,"996103384cdf19179e19243e959bbf8b","cba1b29e32cb17aa46b8ff9e73c7f40b",&handle,"Client/SmartPSS_Win");
    printf("INFO P2P_Init rc=%d\n",rc); if(rc||!handle) { FreeLibrary(dll); return 5; }
    rc=P2P_QueryServerStatus(type,handle); printf("INFO server_status=%d\n",rc);
    rc=P2P_DeviceStatus(type,handle,serial); printf("INFO device_status=%d\n",rc); if(rc) goto cleanup_error;
    char info[8192]={0},salt[512]={0},version[512]={0};
    rc=P2P_GetDeviceInfo(type,handle,serial,sizeof(info),info);
    extract_json_string(info,"randsalt",salt,sizeof(salt)); extract_json_string(info,"devp2pver",version,sizeof(version));
    printf("INFO device_info rc=%d salt=%s version=%s\n",rc,salt[0]?"yes":"no",version);
    for(int i=0;i<map_count;i++) {
        if(i>0) {
            rc=P2P_Init(type,server,8800,"996103384cdf19179e19243e959bbf8b","cba1b29e32cb17aa46b8ff9e73c7f40b",&map_handles[i],"Client/SmartPSS_Win");
            printf("INFO P2P_Init map=%d rc=%d\\n",i,rc);
            if(rc||!map_handles[i]) goto cleanup_error;
            rc=P2P_DeviceStatus(type,map_handles[i],serial);
            printf("INFO device_status map=%d status=%d\\n",i,rc);
            if(rc) goto cleanup_error;
        }
        int requested=maps[i].local_port;
        rc=P2P_Connect(type,map_handles[i],serial,maps[i].remote_port,&maps[i].local_port,user,password,salt,version);
        printf("INFO connect remote=%d requested_local=%d rc=%d local=%d\n",maps[i].remote_port,requested,rc,maps[i].local_port);
        if(rc) goto cleanup_error;
        maps[i].connected=1;
        DWORD deadline=GetTickCount()+90000; int st=11;
        while(!stop_requested&&GetTickCount()<deadline) {
            st=P2P_QueryChannelStatus(type,map_handles[i],maps[i].local_port);
            if(st==0||st==1) break; if(st!=11) { fprintf(stderr,"ERROR invalid channel status remote=%d status=%d\n",maps[i].remote_port,st); goto cleanup_error; }
            Sleep(200);
        }
        if(st!=0&&st!=1) { fprintf(stderr,"ERROR channel ready timeout remote=%d status=%d\n",maps[i].remote_port,st); goto cleanup_error; }
        printf("READY remote=%d local=%d\n",maps[i].remote_port,maps[i].local_port);
    }
    printf("ONLINE all_ports_ready=%d\n",map_count);
    while(!stop_requested) {
        Sleep(5000);
        for(int i=0;i<map_count;i++) {
            int st=P2P_QueryChannelStatus(type,handle,maps[i].local_port);
            printf("ALIVE remote=%d local=%d status=%d\n",maps[i].remote_port,maps[i].local_port,st);
            if(st!=0&&st!=1&&st!=11) { fprintf(stderr,"ERROR channel lost remote=%d status=%d\n",maps[i].remote_port,st); stop_requested=1; rc=10; }
        }
    }
    goto cleanup;
cleanup_error: rc=rc?rc:9;
cleanup:
    for(int i=map_count-1;i>=0;i--) if(maps[i].connected&&map_handles[i]) P2P_Disconnect(type,map_handles[i],serial,maps[i].remote_port,maps[i].local_port);
    for(int i=map_count-1;i>=0;i--) if(map_handles[i]) P2P_UnInit(type,map_handles[i]);
    FreeLibrary(dll); return rc;
}
