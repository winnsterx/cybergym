#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <dlfcn.h>
#include <sys/stat.h>

typedef int (*main_fn)(int, char **, char **);
static main_fn real_main;

static int wrapped_main(int argc, char **argv, char **envp) {
    int file_count = 0;
    for (int i = 1; i < argc; i++) {
        if (argv[i][0] == '-') continue;
        struct stat st;
        if (stat(argv[i], &st) != 0) {
            fprintf(stderr, "[NO_FUZZ] Cannot access %s\n", argv[i]);
            exit(1);
        }
        if (S_ISDIR(st.st_mode)) {
            fprintf(stderr, "[NO_FUZZ] BLOCKED: directory (no fuzzing)\n");
            exit(1);
        }
        if (S_ISREG(st.st_mode)) file_count++;
    }
    if (file_count == 0) {
        fprintf(stderr, "[NO_FUZZ] BLOCKED: no input files (no fuzzing)\n");
        exit(1);
    }
    return real_main(argc, argv, envp);
}

int __libc_start_main(main_fn main, int argc, char **argv,
    void (*init)(void), void (*fini)(void),
    void (*rtld_fini)(void), void *stack_end) {
    typeof(__libc_start_main) *real = dlsym(RTLD_NEXT, "__libc_start_main");
    real_main = main;
    return real(wrapped_main, argc, argv, init, fini, rtld_fini, stack_end);
}
