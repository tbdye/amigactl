# amigactld -- Amiga remote access daemon
# Cross-compilation for m68k-amigaos using m68k-amigaos-gcc

PREFIX  ?= /opt/amiga
CC       = $(PREFIX)/bin/m68k-amigaos-gcc
CFLAGS   = -noixemul -O2 -Wall -Wextra -m68020 -fomit-frame-pointer -MMD -MP
LDFLAGS  = -noixemul

OBJDIR   = obj
TARGET   = amigactld

SRCS = \
	daemon/main.c \
	daemon/config.c \
	daemon/net.c \
	daemon/file.c \
	daemon/exec.c \
	daemon/sysinfo.c

OBJS = $(SRCS:daemon/%.c=$(OBJDIR)/%.o)

.PHONY: all clean

all: $(TARGET)

$(TARGET): $(OBJS)
	$(CC) $(LDFLAGS) -o $@ $^

$(OBJDIR)/%.o: daemon/%.c | $(OBJDIR)
	$(CC) $(CFLAGS) -c -o $@ $<

$(OBJDIR):
	mkdir -p $(OBJDIR)

clean:
	rm -rf $(OBJDIR) $(TARGET)

-include $(wildcard $(OBJDIR)/*.d)
