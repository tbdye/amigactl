/*
 * amigactld -- Configuration file parsing and ACL
 */

#ifndef AMIGACTLD_CONFIG_H
#define AMIGACTLD_CONFIG_H

#include "daemon.h"

/* Initialize config with default values.
 * Port 6800, no ACL (allow all), remote shutdown disabled. */
void config_defaults(struct daemon_config *cfg);

/* Load configuration from file.
 * Missing file is not an error (defaults are used silently).
 * Returns 0 on success, -1 on parse error (diagnostic printed). */
int config_load(struct daemon_config *cfg, const char *path);

/* Check if an IP address is permitted by the ACL.
 * addr is in network byte order (from sin_addr.s_addr).
 * Returns 1 if allowed, 0 if denied.
 * An empty ACL (acl_count == 0) allows all addresses. */
int acl_check(const struct daemon_config *cfg, ULONG addr);

#endif /* AMIGACTLD_CONFIG_H */
