# flatpak-freebsd

Run Flatpak applications on **FreeBSD** using the **Linuxulator**, without installing Flatpak or OSTree.

`flatpak-freebsd` is a lightweight Python implementation for downloading, installing and running applications from Flatpak/OSTree repositories on FreeBSD.

It directly reads an OSTree repository over HTTP, reconstructs the application and its runtime, and launches Linux ELF binaries using the FreeBSD Linuxulator.

> **This project is not a full Flatpak implementation and does not provide Flatpak's security sandbox.**

## Features

* Run Linux Flatpak applications on FreeBSD
* Uses the FreeBSD Linuxulator
* No `flatpak` package required
* No `ostree` package required
* Direct HTTP access to OSTree repositories
* Supports Flathub
* Supports Flathub Beta
* Downloads application runtimes automatically
* Parallel object downloading
* Incremental updates
* Atomic application updates
* Automatic ELF interpreter detection
* Runtime library path construction
* Generates launch wrappers automatically
* Optional per-application XDG directories
* Simple command-line interface
* Written in Python 3

## How it works

Unlike normal Flatpak, this project does not use the Flatpak daemon, OSTree command-line tools, Bubblewrap or Linux namespaces.

The basic workflow is:

```text
Flatpak repository
       │
       │ HTTP
       ▼
flatpak-freebsd.py
       │
       ├── read OSTree refs
       ├── read commit
       ├── read directory tree
       ├── download objects
       ├── reconstruct filesystem
       │
       ▼
Application + Runtime
       │
       ▼
Linux ELF executable
       │
       ▼
Runtime ld-linux
       │
       ▼
FreeBSD Linuxulator
```

The application is reconstructed locally from OSTree objects. When it is started, the program launches the Linux executable using the dynamic loader from the corresponding Flatpak runtime.

## Requirements

* FreeBSD with Linuxulator support
* Python 3
* Internet access
* A compatible Flatpak application and runtime

The Linuxulator must be configured and working before running applications.

Check it with:

```sh
kldstat | grep linux
```

Depending on the FreeBSD configuration, you may need to load the Linuxulator module:

```sh
doas kldload linux
```

## Installation

Clone the repository:

```sh
git clone https://github.com/definitly486/flatpak-freebsd.git
cd flatpak-freebsd
```

Make the script executable:

```sh
chmod +x flatpak-freebsd.py
```

Optionally install it somewhere in your `PATH`:

```sh
doas install -m 755 flatpak-freebsd.py /usr/local/bin/flatpak-freebsd
```

Then:

```sh
flatpak-freebsd --help
```

## Data directory

By default, applications and runtimes are stored in:

```text
~/.local/share/flatpak-freebsd
```

The location can be changed with:

```sh
flatpak-freebsd --root /path/to/root ...
```

or with the environment variable:

```sh
export FLATPAK_FB_ROOT=/path/to/root
```

The data directory contains application data, runtimes, metadata and generated wrappers.

## Usage

### Install an application

```sh
flatpak-freebsd install APP_ID
```

For example:

```sh
flatpak-freebsd install org.example.Application
```

The application runtime is installed automatically if required.

### Start an application

```sh
flatpak-freebsd start APP_ID
```

### Run an application

```sh
flatpak-freebsd run APP_ID
```

The application can also be started through its generated wrapper.

### List installed applications

```sh
flatpak-freebsd list
```

### Show application information

```sh
flatpak-freebsd info APP_ID
```

### Update

Update an installed application:

```sh
flatpak-freebsd update APP_ID
```

Or update installed applications according to the available command options:

```sh
flatpak-freebsd update
```

### Remove an application

```sh
flatpak-freebsd remove APP_ID
```

### Create a link

```sh
flatpak-freebsd link APP_ID
```

### Generate a wrapper

```sh
flatpak-freebsd wrap APP_ID
```

### Check installation

```sh
flatpak-freebsd check APP_ID
```

## Flatpak repositories

The default repository is:

```text
https://dl.flathub.org/repo/
```

Flathub Beta is also supported:

```text
https://dl.flathub.org/beta-repo/
```

The application resolves Flatpak references directly from the repository instead of invoking the `flatpak` or `ostree` command-line tools.

## Application layout

An installed application contains the reconstructed Flatpak filesystem together with metadata describing its runtime and launch information.

Conceptually:

```text
~/.local/share/flatpak-freebsd/
├── apps/
│   └── APP_ID/
│       ├── ...
│       ├── info.json
│       └── env
├── runtimes/
│   └── RUNTIME_ID/
│       └── ...
├── env
└── ...
```

The exact layout is managed by the application and should not normally be modified manually.

## Runtime and ELF execution

Flatpak applications normally expect their own Linux runtime environment.

`flatpak-freebsd` therefore does not simply execute:

```sh
./application
```

Instead, it locates the ELF interpreter supplied by the runtime, for example:

```text
lib/ld-linux-x86-64.so.2
```

and starts the application through that loader.

The equivalent concept is:

```sh
/path/to/runtime/lib/ld-linux-x86-64.so.2 \
    --library-path /path/to/runtime/lib:... \
    /path/to/application
```

This allows the application to use the libraries supplied by its Flatpak runtime while the actual Linux system-call interface is provided by the FreeBSD Linuxulator.

## `/app`

Flatpak applications normally expect their files to be available under:

```text
/app
```

When possible, `flatpak-freebsd` creates:

```text
/compat/linux/app
```

as a link to the installed application.

If the required directory cannot be modified automatically, the program reports the command that must be executed with appropriate privileges.

## Environment

The launcher provides a small set of compatibility defaults intended for running graphical Linux applications on FreeBSD.

Examples include:

```text
GDK_BACKEND=x11
QT_QPA_PLATFORM=xcb
GSK_RENDERER=cairo
LIBGL_ALWAYS_SOFTWARE=1
GTK_A11Y=none
NO_AT_BRIDGE=1
```

WebKit-related settings are also used for applications that cannot use their normal Linux sandbox environment:

```text
WEBKIT_DISABLE_SANDBOX_THIS_IS_DANGEROUS=1
WEBKIT_DISABLE_COMPOSITING_MODE=1
```

These settings are compatibility workarounds and may not be appropriate for every application.

## X11

Graphical applications are currently oriented towards X11.

For example:

```sh
echo $DISPLAY
```

should normally produce something such as:

```text
:0
```

If `DISPLAY` is not set, the launcher warns that a graphical application may not be able to connect to the display server.

## Application isolation

The `--isolate` option can provide separate XDG directories for an application:

```text
~/.var/app/APP_ID/
├── config/
├── data/
├── cache/
└── state/
```

This is **filesystem/environment organization only**.

It is not a security sandbox.

## Security

### Important

`flatpak-freebsd` does **not** provide the security isolation normally associated with Flatpak.

There is no equivalent of Flatpak's complete sandbox based on mechanisms such as:

* Bubblewrap
* Linux namespaces
* seccomp filtering
* restricted device access
* restricted network access
* a dedicated mount namespace
* Flatpak permission enforcement

An installed application can therefore potentially access resources available to the Linuxulator environment according to the permissions of the user running it.

The following setting in particular is intentionally used as a compatibility workaround:

```text
WEBKIT_DISABLE_SANDBOX_THIS_IS_DANGEROUS=1
```

Only run applications that you trust.

## Updates

Applications are synchronized using OSTree commit references.

Updates are performed using a staging directory and atomic directory replacement rather than modifying the currently installed tree in place.

Conceptually:

```text
current application
        │
        ├── download update
        ▼
    APP.new
        │
        ├── successful
        ▼
    atomic rename
        │
        ▼
updated application
```

This helps avoid leaving a partially downloaded application tree after an interrupted update.

## Downloading

OSTree objects are downloaded directly from the repository.

The downloader:

1. Reads the application reference.
2. Obtains the commit.
3. Parses the commit metadata.
4. Traverses the OSTree directory tree.
5. Collects required file objects.
6. Downloads objects in parallel.
7. Decompresses `.filez` objects.
8. Reconstructs files, directories and symlinks.
9. Reuses identical objects where possible.

Downloads use multiple workers to improve performance.

## Architecture

The current implementation contains support for:

```text
x86_64
aarch64
```

The corresponding Linux architecture information and dynamic loader paths are selected automatically.

The primary development and testing target is currently x86_64 FreeBSD.

## Limitations

This project is intentionally much smaller than a complete Flatpak implementation.

Current limitations include:

* No Flatpak sandbox
* No Bubblewrap
* No OSTree command-line dependency
* No Flatpak daemon
* No complete Flatpak permission system
* X11-oriented graphical environment
* Software rendering is enabled by default
* Some applications may require additional compatibility work
* Applications depending on kernel features unavailable through the Linuxulator may not work
* Applications requiring native Flatpak sandbox features may not work
* Hardware acceleration may require application-specific configuration

In particular, successful installation does not guarantee that an arbitrary Flatpak application will run correctly.

## Why?

Running Linux applications on FreeBSD normally involves one of two approaches:

* installing individual Linux packages and manually maintaining their dependencies;
* using a complete Linux VM/container environment.

Flatpak already provides a self-contained application + runtime model.

This project experiments with using that model directly from FreeBSD's Linuxulator:

```text
Flatpak application
        +
Flatpak runtime
        +
FreeBSD Linuxulator
        =
Linux application running directly on FreeBSD
```

The goal is not to reproduce every feature of Flatpak, but to provide a small and practical way to reuse existing Flatpak application bundles.

## Project status

This is an experimental project.

Compatibility will vary considerably between applications.

Simple applications with conventional Linux runtime dependencies are more likely to work than applications that rely heavily on:

* sandboxing
* portals
* system services
* special kernel interfaces
* hardware-specific graphics features
* Flatpak-specific runtime integration

## License

See the `LICENSE` file for the license applicable to this project.

## Contributing

Issues, testing reports and patches are welcome.

Useful reports should include:

* FreeBSD version
* application ID
* runtime ID, if known
* architecture
* command used
* complete error output

For example:

```text
FreeBSD 15.1-STABLE
x86_64
APP_ID=...
RUNTIME=...
```

and:

```sh
flatpak-freebsd run APP_ID
```

with the resulting output.

## Disclaimer

`flatpak-freebsd` is an independent project and is not affiliated with or endorsed by the Flatpak project, Flathub, or their respective maintainers.
