/*
 * amigactld -- File operation command handlers (Phase 2)
 *
 * DIR, STAT, READ, WRITE, DELETE, RENAME, MAKEDIR, PROTECT, SETDATE.
 * Each handler sends its response and returns 0 on success
 * or -1 if the client should be disconnected.
 */

#ifndef AMIGACTLD_FILE_H
#define AMIGACTLD_FILE_H

#include "daemon.h"

int cmd_dir(struct client *c, const char *args);
int cmd_stat(struct client *c, const char *args);
int cmd_read(struct client *c, const char *args);
int cmd_write(struct client *c, const char *args);
int cmd_delete(struct client *c, const char *args);
int cmd_rename(struct client *c, const char *args);
int cmd_makedir(struct client *c, const char *args);
int cmd_protect(struct client *c, const char *args);
int cmd_setdate(struct client *c, const char *args);

#endif /* AMIGACTLD_FILE_H */
