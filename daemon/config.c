/*
 * amigactld -- Configuration file parsing and ACL
 *
 * Parses S:amigactld.conf (or a specified path).  Format:
 *   PORT <number>
 *   ALLOW <ip>
 *   ALLOW_REMOTE_SHUTDOWN YES|NO
 *   ALLOW_REMOTE_REBOOT YES|NO
 *   # comments
 *
 * IP addresses are parsed with sscanf and stored as ULONG in network
 * byte order.  On 68k, host byte order IS network byte order (big-endian),
 * so no htonl() is needed.
 */

#include "config.h"

#include <stdio.h>
#include <string.h>
#include <ctype.h>

void config_defaults(struct daemon_config *cfg)
{
    memset(cfg, 0, sizeof(*cfg));
    cfg->port = DEFAULT_PORT;
    cfg->allow_remote_shutdown = 0;
    cfg->allow_remote_reboot = 0;
    cfg->acl_count = 0;
}

/* Parse a dotted-quad IP address into a ULONG in network byte order.
 * Returns 1 on success, 0 on invalid format. */
static int parse_ip(const char *str, ULONG *out)
{
    int a, b, c, d;
    char trail;

    if (sscanf(str, "%d.%d.%d.%d%c", &a, &b, &c, &d, &trail) != 4)
        return 0;

    if (a < 0 || a > 255 || b < 0 || b > 255 ||
        c < 0 || c > 255 || d < 0 || d > 255)
        return 0;

    /* 68k is big-endian: host byte order == network byte order.
     * This produces the same result as htonl() without needing
     * bsdsocket.library to be open yet. */
    *out = ((ULONG)a << 24) | ((ULONG)b << 16) |
           ((ULONG)c << 8)  | (ULONG)d;
    return 1;
}

/* Skip leading whitespace and return pointer to first non-space char. */
static char *skip_whitespace(char *s)
{
    while (*s == ' ' || *s == '\t')
        s++;
    return s;
}

/* Strip trailing whitespace and newline in-place. */
static void trim_trailing(char *s)
{
    int len = strlen(s);

    while (len > 0 && (s[len - 1] == '\n' || s[len - 1] == '\r' ||
                       s[len - 1] == ' '  || s[len - 1] == '\t'))
        s[--len] = '\0';
}

int config_load(struct daemon_config *cfg, const char *path)
{
    FILE *fp;
    char line[CONFIG_LINE_MAX];
    char *p;
    char *keyword;
    char *value;
    int lineno = 0;

    fp = fopen(path, "r");
    if (!fp) {
        /* Missing config file is not an error -- use defaults */
        return 0;
    }

    while (fgets(line, sizeof(line), fp)) {
        lineno++;
        trim_trailing(line);
        p = skip_whitespace(line);

        /* Skip empty lines and comments */
        if (*p == '\0' || *p == '#')
            continue;

        /* Split into keyword and value */
        keyword = p;
        while (*p && *p != ' ' && *p != '\t')
            p++;

        if (*p) {
            *p++ = '\0';
            value = skip_whitespace(p);
        } else {
            value = p; /* empty value */
        }

        if (stricmp(keyword, "PORT") == 0) {
            int port;

            if (sscanf(value, "%d", &port) != 1 || port < 1 || port > 65535) {
                printf("config: line %d: invalid port \"%s\"\n",
                       lineno, value);
                fclose(fp);
                return -1;
            }
            cfg->port = port;
        } else if (stricmp(keyword, "ALLOW") == 0) {
            ULONG addr;

            if (cfg->acl_count >= MAX_ACL_ENTRIES) {
                printf("config: line %d: too many ALLOW entries (max %d)\n",
                       lineno, MAX_ACL_ENTRIES);
                fclose(fp);
                return -1;
            }
            if (!parse_ip(value, &addr)) {
                printf("config: line %d: invalid IP address \"%s\"\n",
                       lineno, value);
                fclose(fp);
                return -1;
            }
            cfg->acl[cfg->acl_count].addr = addr;
            cfg->acl_count++;
        } else if (stricmp(keyword, "ALLOW_REMOTE_SHUTDOWN") == 0) {
            if (stricmp(value, "YES") == 0) {
                cfg->allow_remote_shutdown = 1;
            } else if (stricmp(value, "NO") == 0) {
                cfg->allow_remote_shutdown = 0;
            } else {
                printf("config: line %d: ALLOW_REMOTE_SHUTDOWN must be "
                       "YES or NO, got \"%s\"\n", lineno, value);
                fclose(fp);
                return -1;
            }
        } else if (stricmp(keyword, "ALLOW_REMOTE_REBOOT") == 0) {
            if (stricmp(value, "YES") == 0) {
                cfg->allow_remote_reboot = 1;
            } else if (stricmp(value, "NO") == 0) {
                cfg->allow_remote_reboot = 0;
            } else {
                printf("config: line %d: ALLOW_REMOTE_REBOOT must be "
                       "YES or NO, got \"%s\"\n", lineno, value);
                fclose(fp);
                return -1;
            }
        } else {
            printf("config: line %d: unknown keyword \"%s\"\n",
                   lineno, keyword);
            fclose(fp);
            return -1;
        }
    }

    fclose(fp);
    return 0;
}

int acl_check(const struct daemon_config *cfg, ULONG addr)
{
    int i;

    /* Empty ACL = allow all */
    if (cfg->acl_count == 0)
        return 1;

    for (i = 0; i < cfg->acl_count; i++) {
        if (cfg->acl[i].addr == addr)
            return 1;
    }

    return 0;
}
