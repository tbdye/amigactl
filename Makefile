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
	daemon/sysinfo.c \
	daemon/arexx.c \
	daemon/tail.c \
	daemon/trace.c

OBJS = $(SRCS:daemon/%.c=$(OBJDIR)/%.o)

# atrace resident module (separate binary)
ATRACE_SRCS = \
	atrace/main.c \
	atrace/ringbuf.c \
	atrace/funcs.c \
	atrace/stub_gen.c

ATRACE_OBJS = $(ATRACE_SRCS:atrace/%.c=$(OBJDIR)/atrace_%.o)
ATRACE_TARGET = atrace_loader

.PHONY: all clean

all: $(TARGET) $(ATRACE_TARGET)

$(TARGET): $(OBJS)
	$(CC) $(LDFLAGS) -o $@ $^

$(ATRACE_TARGET): $(ATRACE_OBJS)
	$(CC) $(LDFLAGS) -o $@ $^

$(OBJDIR)/%.o: daemon/%.c | $(OBJDIR)
	$(CC) $(CFLAGS) -c -o $@ $<

$(OBJDIR)/atrace_%.o: atrace/%.c | $(OBJDIR)
	$(CC) $(CFLAGS) -c -o $@ $<

$(OBJDIR):
	mkdir -p $(OBJDIR)

clean:
	rm -rf $(OBJDIR) $(TARGET) $(ATRACE_TARGET)

-include $(wildcard $(OBJDIR)/*.d)
