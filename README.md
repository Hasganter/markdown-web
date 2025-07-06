# (VERY MUCH UNSTABLE-ALPHA) MDWeb Project Documentation

This document provides a comprehensive overview of the MDWeb project, a self-contained, process-managed web application framework designed for easily converting Markdown content into dynamic, full-featured websites with custumizable templates.

It provides a way for non-programmers to super easily add a website page on the fly without much of a think. Of course, the templates do need to be setup by a trained admin.

## Table of Contents

- [(VERY MUCH UNSTABLE-ALPHA) MDWeb Project Documentation](#very-much-unstable-alpha-mdweb-project-documentation)
  - [Table of Contents](#table-of-contents)
  - [1. Overview](#1-overview)
    - [What is MDWeb?](#what-is-mdweb)
    - [Key Features](#key-features)
  - [2. Core Concepts \& Architecture](#2-core-concepts--architecture)
    - [System Architecture Diagram](#system-architecture-diagram)
    - [System Startup Diagram](#system-startup-diagram)
    - [The Process Supervisor Model](#the-process-supervisor-model)
    - [The Content Pipeline](#the-content-pipeline)
    - [Automated Dependency Management](#automated-dependency-management)
    - [Configuration Hierarchy](#configuration-hierarchy)
  - [3. Getting Started](#3-getting-started)
    - [Prerequisites](#prerequisites)
    - [Installation \& First Run](#installation--first-run)
  - [4. The Management Console (`run.bat`)](#4-the-management-console-runbat)
    - [Running the Console](#running-the-console)
    - [Available Commands](#available-commands)
  - [5. Creating Content](#5-creating-content)
    - [The `_ROOT-INDEX_` Directory](#the-_root-index_-directory)
    - [Creating a Basic Page](#creating-a-basic-page)
    - [Creating Subdomains](#creating-subdomains)
    - [YAML Front Matter Deep Dive](#yaml-front-matter-deep-dive)
    - [Working with Media Assets](#working-with-media-assets)
    - [Creating and Using Custom Templates](#creating-and-using-custom-templates)
  - [6. Configuration In-Depth](#6-configuration-in-depth)
    - [The `.env` File](#the-env-file)
    - [The `settings.py` File](#the-settingspy-file)
    - [Runtime Configuration with `overrides.json`](#runtime-configuration-with-overridesjson)
  - [7. Observability: Logging \& Monitoring](#7-observability-logging--monitoring)
    - [Structured Logging with Grafana Loki](#structured-logging-with-grafana-loki)
    - [Real-time Log Tailing](#real-time-log-tailing)
    - [Exporting Logs to Excel](#exporting-logs-to-excel)
  - [8. Technical Deep Dive: Project Structure](#8-technical-deep-dive-project-structure)
    - [High-Level Directory Structure](#high-level-directory-structure)
    - [Module Breakdown (`src/`)](#module-breakdown-src)

---

## 1. Overview

### What is MDWeb?

MDWeb is a powerful, all-in-one solution for building and serving websites from simple Markdown files. It goes far beyond a simple script by providing a robust, production-ready environment that includes:

*   A high-performance **Nginx** reverse proxy for handling public traffic, rate-limiting, and security.
*   A modern **Starlette/Hypercorn** ASGI backend to serve dynamic content.
*   An intelligent **Process Management** system that supervises all services, automatically restarting them on failure.
*   A sophisticated **Content Pipeline** that watches for file changes, converts Markdown and media on-the-fly, and stores the resulting HTML in a high-performance SQLite database.
*   **Zero-Setup Dependency Management**, which automatically downloads, installs, and updates external binaries like Nginx and FFmpeg.

It is designed for developers and content creators who want the simplicity of a static site generator with the power and resilience of a dynamic, multi-process application.

### Key Features

*   **Process Supervision:** Critical services (Nginx, ASGI server, content converter) are monitored and automatically restarted upon failure.
*   **Live Content Reloading:** The `_ROOT-INDEX_` directory is watched for changes. Any modification to `.md` or media files triggers an automatic, parallelized re-processing.
*   **Markdown-to-HTML Conversion:** Pages are written in Markdown with powerful YAML front matter for metadata and configuration.
*   **Automated Asset Optimization:** Media files (images, video, audio) are automatically converted to web-optimized formats (AVIF, WebM, MP3) using FFmpeg.
*   **Subdomain Support:** Easily create different sections of your site or multi-tenant pages by creating directories with a `.` prefix (e.g., `_ROOT-INDEX_/.blog`).
*   **Dynamic Configuration:** Key settings can be viewed and modified at runtime via the management console without editing source files.
*   **Built-in Observability:** Integrates with Grafana Loki for structured, centralized logging, and provides tools for real-time log viewing and Excel exports.
*   **Self-Contained & Portable:** The system manages its own external dependencies, making setup on a new machine as simple as running a single script.
*   **Secure by Default:** Implements security headers, Nginx rate-limiting, and a fallback Python-level DDoS protection middleware.

## 2. Core Concepts & Architecture

Understanding the architecture is key to leveraging the full power of MDWeb. The system is composed of several independent but interconnected processes that are managed by a central supervisor.

### System Architecture Diagram

```mermaid
---
config:
  layout: elk
  elk:
    nodePlacementStrategy: BRANDES_KOEPF
---

graph LR
    %% USER INTERACTION
    subgraph "👤 User"
        User[End User]
    end

    %% EXTERNAL
    subgraph "🌐 Public-Facing"
        Nginx[Nginx<br>Reverse Proxy]
    end

    %% INTERNAL
    subgraph "🧩 Internal Suite"
        subgraph "📡 Web Server"
            ASGI["ASGI Server<br>(Starlette/Hypercorn)"]
        end

        subgraph "🛠️ Content Processing"
            Watchdog["Watchdog<br>(Monitors Files)"]
            ContentConverter["Content Converter<br>(Markdown, HTML, Media)"]
        end

        subgraph "🎛️ Orchestration"
            Supervisor["Supervisor<br>(Manages Services)"]
        end
    end

    %% STORAGE
    subgraph "🗂️ Files & DB"
        SourceFiles[/_ROOT-INDEX_/]
        Assets["/_ROOT-INDEX_/.assets"]
        BinDir["/bin/assets/<br>Optimized Media"]
        ContentDB[(content.db)]
        LogDB[(app_logs.db)]
    end

    %% Logging
    subgraph "📈 Logging"
        Alloy["Grafana Alloy"]
        Loki["Grafana Loki"]
    end

    %% FLOW
    User -->|HTTPS Request| Nginx
    Nginx -->|Static Assets| BinDir
    Nginx -->|Proxy Request| ASGI
    ASGI -->|Reads HTML| ContentDB

    Watchdog -->|Watches| SourceFiles
    Watchdog -->|Watches| Assets
    Watchdog -->|Triggers| ContentConverter
    ContentConverter -->|Updates| ContentDB
    ContentConverter -->|Optimizes Media| BinDir

    Supervisor -->|Manages| Nginx
    Supervisor -->|Manages| ASGI
    Supervisor -->|Manages| ContentConverter
    Supervisor -->|Manages| Loki
    Supervisor -->|Manages| Alloy

    Nginx -->|stdout| Supervisor
    ASGI -->|stdout| Supervisor
    ContentConverter -->|stdout| Supervisor
    Supervisor -->|Stores Logs| LogDB
    Supervisor -->|Sends Logs| Alloy
    Alloy -->|Pushes Logs| Loki

    %% STYLING
    classDef userClass fill:#e1f5fe,stroke:#01579b,stroke-width:2px
    classDef publicClass fill:#f3e5f5,stroke:#4a148c,stroke-width:2px
    classDef webServerClass fill:#e8f5e8,stroke:#1b5e20,stroke-width:2px
    classDef contentClass fill:#fff3e0,stroke:#e65100,stroke-width:2px
    classDef orchestrationClass fill:#f1f8e9,stroke:#33691e,stroke-width:2px
    classDef storageClass fill:#fce4ec,stroke:#880e4f,stroke-width:2px
    classDef loggingClass fill:#e8eaf6,stroke:#1a237e,stroke-width:2px

    class User userClass
    class Nginx publicClass
    class ASGI webServerClass
    class Watchdog,ContentConverter contentClass
    class Supervisor orchestrationClass
    class SourceFiles,Assets,BinDir,ContentDB,LogDB storageClass
    class Alloy,Loki loggingClass
```

### System Startup Diagram

```mermaid
---
config:
  layout: elk
  elk:
    nodePlacementStrategy: BRANDES_KOEPF
---

graph TD
    %% USER INTERACTION
    subgraph "👤 User Interaction"
        User[User runs run.bat]
        UserArgs{{"Arguments provided?"}}
        UserFresh["run.bat fresh"]
        UserClear["run.bat clear"]
        UserHelp["run.bat help"]
        UserNormal["run.bat (no args)"]
    end

    %% BATCH SCRIPT PROCESSING
    subgraph "🏗️ Environment Setup (run.bat)"
        CheckPython["Check Python Installation"]
        CreateEnv["Create .env if missing"]
        CreateVenv["Create/Activate venv"]
        InstallDeps["Install requirements.txt"]
        CreateDirs["Create necessary directories"]
        SetPyCache["Set PYTHONPYCACHEPREFIX"]
    end

    %% PYTHON APPLICATION ENTRY
    subgraph "🐍 Python Entry Point (main.py)"
        LoadConfig["Load Configuration Hierarchy"]
        SetupBasicLog["Setup Basic Console Logging"]
        CheckMode{{"Interactive or<br>Command Mode?"}}
        OneOffCmd["Execute Single Command"]
        InteractiveConsole["Interactive Management Console"]
    end

    %% DEPENDENCY MANAGEMENT
    subgraph "📦 Dependency Management"
        FirstRun{{"First Run?"}}
        DownloadExternal["Download External Dependencies<br>(Nginx, FFmpeg, Grafana tools)"]
        ApplyPending["Apply Pending Updates"]
        WriteConfigs["Write Configuration Files<br>(nginx.conf, alloy config)"]
    end

    %% DATABASE INITIALIZATION
    subgraph "🗄️ Database Setup"
        InitLogDB["Initialize Log Database<br>(app_logs.db)"]
        InitContentDB["Initialize Content Database<br>(content.db)"]
        ScanContent["Initial Content Scan<br>(Blocking)"]
        ScanAssets["Initial Asset Processing<br>(Blocking)"]
    end

    %% PROCESS LAUNCHING
    subgraph "🚀 Process Launch Sequence"
        LaunchLoki["Launch Grafana Loki<br>(if enabled)"]
        LaunchAlloy["Launch Grafana Alloy<br>(if enabled)"]
        LaunchConverter["Launch Content Converter<br>(Background file watcher)"]
        LaunchASGI["Launch ASGI Server<br>(Hypercorn/Starlette)"]
        HealthCheck["ASGI Health Check"]
        LaunchNgrok["Launch Ngrok<br>(if enabled)"]
        LaunchNginx["Launch Nginx<br>(Reverse Proxy)"]
        StartLogTailing["Start Nginx Log Tailing Thread"]
        LaunchSupervisor["Launch Supervisor Process<br>(Detached)"]
    end

    %% SUPERVISOR OPERATIONS
    subgraph "🎛️ Supervisor Process"
        SupervisorStart["Supervisor Detaches from Console"]
        MonitorLoop["Monitor Critical Processes<br>(nginx, asgi_server, content_converter)"]
        RestartLogic["Auto-restart Failed Processes<br>(with cooldown & attempt limits)"]
    end

    %% RUNTIME OPERATIONS
    subgraph "⚡ Runtime Operations"
        ServeRequests["Serve User Requests"]
        WatchFiles["Watch _ROOT-INDEX_ for Changes"]
        ProcessMedia["Auto-process Media Files"]
        LogAggregation["Aggregate Logs to Grafana"]
        UpdateCheck["Background Dependency Updates"]
    end

    %% FINAL STATES
    subgraph "✅ Final States"
        AppRunning["Application Running<br>& Supervised"]
        ConsoleReady["Management Console Ready"]
        ExitEarly["Exit Without Starting<br>(help, clear commands)"]
    end

    %% FLOW CONNECTIONS
    User --> UserArgs
    UserArgs -->|fresh| UserFresh
    UserArgs -->|clear/clog/cbin| UserClear
    UserArgs -->|help| UserHelp
    UserArgs -->|no args or other| UserNormal

    UserFresh --> CheckPython
    UserClear --> ExitEarly
    UserHelp --> ExitEarly
    UserNormal --> CheckPython

    CheckPython --> CreateEnv
    CreateEnv --> CreateVenv
    CreateVenv --> InstallDeps
    InstallDeps --> CreateDirs
    CreateDirs --> SetPyCache
    SetPyCache --> LoadConfig

    LoadConfig --> SetupBasicLog
    SetupBasicLog --> CheckMode
    CheckMode -->|Command| OneOffCmd
    CheckMode -->|Interactive| InteractiveConsole

    %% Start Command Flow
    OneOffCmd -.->|start command| FirstRun
    InteractiveConsole -.->|start command| FirstRun

    FirstRun -->|Yes| DownloadExternal
    FirstRun -->|No| ApplyPending
    DownloadExternal --> ApplyPending
    ApplyPending --> WriteConfigs
    WriteConfigs --> InitLogDB

    InitLogDB --> InitContentDB
    InitContentDB --> ScanContent
    ScanContent --> ScanAssets
    ScanAssets --> LaunchLoki

    LaunchLoki --> LaunchAlloy
    LaunchAlloy --> LaunchConverter
    LaunchConverter --> LaunchASGI
    LaunchASGI --> HealthCheck
    HealthCheck --> LaunchNgrok
    LaunchNgrok --> LaunchNginx
    LaunchNginx --> StartLogTailing
    StartLogTailing --> LaunchSupervisor

    LaunchSupervisor --> SupervisorStart
    SupervisorStart --> MonitorLoop
    MonitorLoop --> RestartLogic
    RestartLogic --> MonitorLoop

    LaunchSupervisor --> AppRunning
    AppRunning --> ServeRequests
    AppRunning --> WatchFiles
    AppRunning --> ProcessMedia
    AppRunning --> LogAggregation
    AppRunning --> UpdateCheck

    InteractiveConsole --> ConsoleReady

    %% STYLING
    classDef userClass fill:#e1f5fe,stroke:#01579b,stroke-width:2px
    classDef batchClass fill:#f3e5f5,stroke:#4a148c,stroke-width:2px
    classDef pythonClass fill:#e8f5e8,stroke:#1b5e20,stroke-width:2px
    classDef depClass fill:#fff3e0,stroke:#e65100,stroke-width:2px
    classDef dbClass fill:#fce4ec,stroke:#880e4f,stroke-width:2px
    classDef processClass fill:#e0f2f1,stroke:#004d40,stroke-width:2px
    classDef supervisorClass fill:#f1f8e9,stroke:#33691e,stroke-width:2px
    classDef runtimeClass fill:#e8eaf6,stroke:#1a237e,stroke-width:2px
    classDef finalClass fill:#ffebee,stroke:#b71c1c,stroke-width:2px

    class User,UserArgs,UserFresh,UserClear,UserHelp,UserNormal userClass
    class CheckPython,CreateEnv,CreateVenv,InstallDeps,CreateDirs,SetPyCache batchClass
    class LoadConfig,SetupBasicLog,CheckMode,OneOffCmd,InteractiveConsole pythonClass
    class FirstRun,DownloadExternal,ApplyPending,WriteConfigs depClass
    class InitLogDB,InitContentDB,ScanContent,ScanAssets dbClass
    class LaunchLoki,LaunchAlloy,LaunchConverter,LaunchASGI,HealthCheck,LaunchNgrok,LaunchNginx,StartLogTailing,LaunchSupervisor processClass
    class SupervisorStart,MonitorLoop,RestartLogic supervisorClass
    class ServeRequests,WatchFiles,ProcessMedia,LogAggregation,UpdateCheck runtimeClass
    class AppRunning,ConsoleReady,ExitEarly finalClass
```

### The Process Supervisor Model

The core of MDWeb's resilience comes from its process management, handled by `src/local/manager.py` and kicked off by `src/local/supervisor_entry.py`.

1.  **Main Console (`main.py`):** When you run `start`, it launches all necessary processes. The last process it launches is the **Supervisor**.
2.  **Supervisor Process:** This lightweight process detaches from the console and becomes the parent of all other services. Its sole job is to monitor the PIDs of the critical processes (`nginx`, `asgi_server`, `content_converter`).
3.  **Health Monitoring:** The supervisor periodically checks if the monitored processes are running.
4.  **Automatic Restart:** If a critical process crashes, the supervisor will attempt to restart it. It includes a cooldown and attempt-limit mechanism to prevent rapid-fire restarts of a persistently failing service.
5.  **Graceful Shutdown:** When you run `stop`, a signal is sent to the supervisor. It then orchestrates a graceful shutdown: Nginx is told to quit gracefully, other processes are terminated, and any stubborn processes are killed after a timeout.

This model ensures the application can recover from unexpected crashes and provides a clean, reliable way to manage the application's lifecycle.

### The Content Pipeline

The content pipeline is the journey of your content from a simple text file to a webpage served to the user.

1.  **Creation:** You create a directory inside `_ROOT-INDEX_` and add a content file (e.g., `index.md`). Media files are placed in `_ROOT-INDEX_/.assets`.
2.  **Detection (`watchdog`):** The `ContentConverter` process uses the `watchdog` library to monitor the `_ROOT-INDEX_` directory for any file system changes (creation, modification, deletion).
3.  **Processing (`ContentConverter`):**
    *   When a change is detected, a task is sent to a multiprocessing pool.
    *   For `.md`/`.html` files, the worker process reads the file, parses the YAML front matter, converts Markdown to an HTML fragment, and uses a template (`src/templates/default.py` or a custom one) to generate the full-page HTML.
    *   For media files, the worker uses **FFmpeg** to convert the asset into a web-optimized format (e.g., `my-image.jpg` -> `my-image.jpg.avif`) and saves it in the `bin/assets` directory.
4.  **Storage (`ContentDBManager`):** The final HTML, title, and other metadata (like allowed HTTP methods) are written to the `bin/content.db` SQLite database. It uses a unique `path_key` (e.g., `main:/about`) to identify the page. The database is run in **WAL (Write-Ahead Logging) mode**, which is critical for allowing the ASGI server to read from it while the ContentConverter is simultaneously writing to it.
5.  **Serving (`ASGI Server`):**
    *   When a user requests a page (e.g., `http://localhost:8080/about`), Nginx proxies the request to the Starlette ASGI server.
    *   The server determines the `path_key` from the request's host and path.
    *   It performs a fast, read-only query on `content.db` to fetch the pre-rendered HTML.
    *   The HTML is sent back to the user.

This asynchronous, database-backed pipeline is extremely efficient. The expensive work (Markdown conversion, templating, asset optimization) is done in the background, so user requests are served instantly from the database.

### Automated Dependency Management

A standout feature is the system's ability to manage its own external binary dependencies, handled by `src/local/externals.py`.

*   **First Run:** On the first run, the `DependencyManager` checks the `external/` directory. If dependencies are missing, it:
    1.  Reads the `EXTERNAL_DEPENDENCIES` dictionary in `settings.py`.
    2.  Fetches the latest version number for each dependency (e.g., from the GitHub API).
    3.  Downloads the correct `.zip` archive for the OS.
    4.  Extracts it into the `external/` directory.
    5.  Saves the version information in a `.version` or `.versions.json` file.
*   **Updates:** Periodically, a background thread checks for new versions. If an update is found, it's downloaded to a temporary directory. The update is automatically applied the next time the application is started.
*   **Recovery:** The system archives old versions of dependencies in the `external/.old/` directory. If an update causes problems, the `recover <dependency>` command allows you to roll back to a previously working version.

### Configuration Hierarchy

The application uses a layered configuration system to provide flexibility and security, managed by `src/local/config.py`. The order of precedence is:

1.  **`settings.py` (Lowest Precedence):** Contains the hardcoded default values and core application paths. This is the source of truth.
2.  **`.env` File:** Loaded by `python-dotenv`. Overrides values from `settings.py`. Ideal for environment-specific settings like ports, domains, and API keys.
3.  **`bin/overrides.json` (Highest Precedence):** Contains runtime-modifiable settings. These values are changed via the `config set` and `config save` commands in the management console. Only settings explicitly listed in `MODIFIABLE_SETTINGS` can be overridden this way.

## 3. Getting Started

### Prerequisites

*   **Python 3.9+:** Ensure Python is installed and accessible from your command line as `python` or `python3`.
*   **Git:** To clone the project repository.

### Installation & First Run

1.  **Clone the Repository:**
    ```bash
    git clone <your-repository-url>
    cd <project-directory>
    ```

2.  **Run the Setup Script:**
    Execute the `run.bat` script from your terminal.
    ```bash
    run.bat
    ```

**What Happens on the First Run?**

The `run.bat` script is an intelligent bootstrapper that will:
*   Check for a valid Python installation.
*   Create a default `.env` file if one doesn't exist.
*   Create a Python virtual environment in the `venv/` directory.
*   Activate the virtual environment.
*   Install all required Python packages from `reqs.txt`.
*   Create necessary directories (`bin/`, `logs/`, `_ROOT-INDEX_/`, etc.).
*   Launch the interactive management console.

Once in the console, you can start the application.

```
> start
```
The first time you run `start`, the `DependencyManager` will download and set up Nginx, FFmpeg, and other external tools. This may take a few minutes. Subsequent starts will be much faster.

Once started, the website will be available at the domain specified in your `.env` file (default: `http://localhost:8080`).

## 4. The Management Console (`run.bat`)

The primary way to interact with the application is through the management console, launched by `run.bat` or by running `python -m src.main`.

### Running the Console

*   **Interactive Mode:** `run.bat`
*   **One-off Command:** `run.bat <command> [args...]` (e.g., `run.bat status`)
*   **Fresh Start:** `run.bat fresh` (Deletes `bin` and `logs` before starting the console, useful for a clean slate).

### Available Commands

| Command | Description | Example Usage |
| :--- | :--- | :--- |
| `start` | Starts all application services (Nginx, ASGI, etc.) in the background. | `> start` |
| `stop` | Gracefully stops all running application services. | `> stop` |
| `restart` | Stops and then immediately restarts all services. | `> restart` |
| `status` | Shows the current status (Running/Stopped) and PID of each service. | `> status` |
| `logs` | Tails the application logs in real-time. Press any key to stop tailing. | `> logs` |
| `export-logs` | Exports the contents of the log database to a styled Excel file. | `> export-logs my_app_logs.xlsx` |
| `config show` | Displays all runtime-modifiable settings and their current values. | `> config show` |
| `config set` | Temporarily changes a setting for the current session. | `> config set LOG_BUFFER_SIZE 200` |
| `config save` | Persists any temporary settings changes to `bin/overrides.json`. | `> config save` |
| `check-config`| Validates that all external binary paths in the configuration are correct. | `> check-config` |
| `recover` | Interactively recovers an archived version of a dependency. **Must be run while the server is stopped.** | `> recover nginx` |
| `verbose` | Toggles verbose (DEBUG level) logging in the console. | `> verbose` |
| `help` | Displays a list of all available commands. | `> help` |
| `exit` | Exits the management console. | `> exit` |

## 5. Creating Content

All user-facing content is managed within the `_ROOT-INDEX_` directory.

### The `_ROOT-INDEX_` Directory

This is the source root for all your website's content. The structure inside this directory directly maps to the URL structure of your website.

```
_ROOT-INDEX_/
├── .assets/              # Global media files go here
│   ├── background.jpg
│   └── company-video.mp4
├── .blog/                # Becomes the 'blog' subdomain (blog.localhost:8080)
│   ├── index.md          # Content for blog.localhost:8080/
│   └── .assets/          # Subdomain-specific assets (not currently implemented, but a good practice)
│       └── post-image.png
├── about/                # Becomes localhost:8080/about
│   └── about.md          # Content file. Name must match the parent dir.
├── services/             # Becomes localhost:8080/services
│   └── services.md       # Content for localhost:8080/services
└── index.md              # Content for the main domain root (localhost:8080/)
```

### Creating a Basic Page

Let's create an "About Us" page at the URL `http://localhost:8080/about`.

1.  **Create a Directory:** Inside `_ROOT-INDEX_`, create a new folder named `about`.
    ```
    _ROOT-INDEX_/
    └── about/
    ```

2.  **Create the Content File:** Inside the `about` directory, create a file named `about.md`. **The canonical content file for a sub-directory must be named the same as the directory itself.**
    ```
    _ROOT-INDEX_/
    └── about/
        └── about.md
    ```

3.  **Add Content to `about.md`:**

    ```markdown
    ---
    CONTEXT:
      title: "About Our Awesome Company"
    ---
    
    # About Us
    
    We are a company dedicated to making amazing things. This page was generated automatically from a simple Markdown file!
    
    Here are some of our values:
    - Simplicity
    - Power
    - Elegance
    ```

4.  **Save the File:** If the application is running, the `ContentConverter` will automatically detect the new file, process it, and add it to the database. You can immediately visit `http://localhost:8080/about` to see your new page.

### Creating Subdomains

To create a page on a subdomain (e.g., `blog.localhost:8080`), simply create a directory inside `_ROOT-INDEX_` that starts with a dot (`.`).

1.  **Create a Directory:** `_ROOT-INDEX_/.blog`
2.  **Create a Content File:** Inside `.blog`, create `index.md`. This will be the content for the subdomain's root URL.
    ```markdown
    ---
    CONTEXT:
      title: "My Awesome Blog"
      header_title: "MyBlog" # Override the header title for this subdomain
    ---
    # Welcome to the Blog!
    
    This is the first post on our new subdomain.
    ```
3.  **Save and Visit:** The page will be available at `http://blog.localhost:8080`.

### YAML Front Matter Deep Dive

The YAML block at the top of your `.md` or `.html` files, enclosed in `---`, gives you powerful control over each page.

```yaml
---
# CONTEXT: Variables passed directly to the HTML template.
CONTEXT:
  title: "My Page Title"                 # Overrides the <title> tag.
  header_title: "MySite"                 # Overrides the site name in the header.
  footer_credit: "My Custom Credit"      # Overrides the name in the footer.
  background_link: "http://assets.localhost:8080/custom-bg.jpg" # Custom background image for this page.
  services_list: |                       # Special multiline string for the services section.
    [Service One]
    This is the description for the first service.
    [Service Two]
    Description for the second, very cool service.

# TEMPLATE: Defines a custom Python class to use for rendering.
TEMPLATE:
  module: "custom_template"              # The python module name in src/templates/
  class: "MyCoolTemplate"                # The class name within that module.

# ALLOWED_METHODS: A list of HTTP methods this endpoint will respond to.
ALLOWED_METHODS: ["GET", "POST"]         # Allows this page to accept POST requests.
---
# The rest of your Markdown or HTML content goes here...
```

### Working with Media Assets

All media files are processed by FFmpeg for web optimization.

1.  **Place Files:** Add your source media (e.g., `.jpg`, `.mp4`, `.mp3`) to the `_ROOT-INDEX_/.assets/` directory.
2.  **Automatic Conversion:** The system will automatically detect and convert them.
    *   Images (`.png`, `.jpg`) -> AVIF (`.avif`)
    *   Videos (`.mp4`, `.mov`) -> WebM (`.webm`)
    *   Audio (`.mp3`, `.wav`) -> MP3 (`.mp3`)
    The converted files are placed in `bin/assets/`.
3.  **Referencing Assets:** In your content, you should reference assets using the asset subdomain. Nginx is configured to serve these directly.
    *   **Original Filename:** Nginx will automatically serve the optimized version. For example, if you have `my-image.jpg` in your `.assets` folder, you reference it as `http://assets.localhost:8080/my-image.jpg`. Nginx tries to serve `bin/assets/my-image.jpg.avif` first. If that fails (or if the `ori=true` query param is used), it will ask the Python app for the original file.
    *   **Example:**
        ```markdown
        ![My Image](http://assets.localhost:8080/my-image.jpg)
        ```

### Creating and Using Custom Templates

While `src/templates/default.py` is powerful, you can create your own.

1.  **Create a Template File:** Create a new Python file in `src/templates/`, for example, `minimalist.py`.
2.  **Define the Template Class:** In `minimalist.py`, create a class with a `convert` method.

    ```python
    # src/templates/minimalist.py
    from html import escape
    from typing import Dict, Any

    class MinimalistTemplate:
        def convert(self, markdown_html_content: str, context: Dict[str, Any]) -> str:
            title = escape(context.get("title", "Minimalist Page"))
            return f"""
            <!DOCTYPE html>
            <html>
            <head><title>{title}</title></head>
            <body>
                <main>{markdown_html_content}</main>
            </body>
            </html>
            """
    ```

3.  **Use the Template:** In your page's YAML front matter, specify the module and class name.

    ```yaml
    ---
    CONTEXT:
      title: "A Minimal Page"
    TEMPLATE:
      module: "minimalist"
      class: "MinimalistTemplate"
    ---

    This content will be rendered using the minimalist template.
    ```

## 6. Configuration In-Depth

### The `.env` File

This is the primary file for user-level configuration. The `run.bat` script creates a default version if it's missing.

*   `MYAPP_DOMAIN`: The main domain the site will respond to.
*   `NGINX_PORT`: The public-facing port Nginx listens on.
*   `ASGI_PORT`: The internal port the Python web server listens on.
*   `ASGI_WORKERS`: Number of Hypercorn worker processes. `0` for auto-calculation.
*   `NGROK_ENABLED`: Set to `True` to enable Ngrok tunneling for development.
*   `LOKI_ENABLED`: Set to `True` to enable logging to a Grafana Loki instance.

### The `settings.py` File

This file is the single source of truth for all configuration defaults, file paths, and system constants. You should generally not edit this file unless you are fundamentally changing the project structure. It's an excellent reference for understanding where the application expects to find files and what settings are available.

### Runtime Configuration with `overrides.json`

For settings that need to be changed frequently without a full application restart (like logging levels or scan intervals), the project uses a special override system.

*   The `MODIFIABLE_SETTINGS` set in `settings.py` defines which keys can be changed at runtime.
*   The `config set <KEY> <VALUE>` command changes a setting in memory for the current session.
*   The `config save` command writes these in-memory changes to `bin/overrides.json`.
*   On the next application start, these overrides are loaded and take the highest precedence.

## 7. Observability: Logging & Monitoring

### Structured Logging with Grafana Loki

If `LOKI_ENABLED` is `True`, the system provides enterprise-grade observability.

*   **Nginx Logs:** The Nginx configuration is set to output logs in a structured JSON format to `stdout`.
*   **Python Logs:** A custom `LokiHandler` batches logs from the Python application.
*   **Grafana Alloy:** The `alloy` process is launched to tail the Nginx logs and forward them to Loki. The Python handler sends its logs directly.
*   This provides a unified view of all request and application logs in a Grafana dashboard, with labels for `job`, `level`, `hostname`, etc.

### Real-time Log Tailing

The `logs` command provides an interactive way to monitor the application directly from the console.

1.  It first displays the last 50 log entries from the `logs/app_logs.db` database.
2.  It then enters a "tail" mode, polling the database for new entries and printing them as they arrive.

### Exporting Logs to Excel

For offline analysis or reporting, the `export-logs [filename]` command queries the entire log database and creates a beautifully styled and formatted Excel spreadsheet with conditional coloring for different log levels.

## 8. Technical Deep Dive: Project Structure

### High-Level Directory Structure

| Path | Purpose |
| :--- | :--- |
| `bin/` | **Generated/Runtime Data.** Contains compiled configs, databases, PID files, and optimized assets. Auto-created by the application. Should be in `.gitignore`. |
| `external/` | **Managed Dependencies.** External binaries like Nginx, FFmpeg, Grafana Loki, and Grafana Alloy are automatically downloaded and stored here. |
| `logs/` | **Application Logs.** Contains the `app_logs.db` SQLite database for structured logging. |
| `src/` | **Source Code.** The core Python application logic organized into logical modules. |
| `_ROOT-INDEX_/` | **Content Source.** All user-created Markdown, HTML, and media files. The directory structure maps directly to URL paths. |
| `temp/` | **Temporary Files.** Working directory for file processing and temporary assets. |
| `tests/` | **Test Suite.** Unit and integration tests for the application. |
| `venv/` | **Python Virtual Environment.** Isolated Python environment with all dependencies. |
| `.env` | **Environment Configuration.** User-configurable settings like ports, domains, and feature flags. |
| `reqs.txt` | **Python Dependencies.** All required Python packages for the application. |
| `run.bat` | **Main Entry Point.** Intelligent bootstrapper that handles setup, virtual environment creation, and application management. |

### Module Breakdown (`src/`)

*   **Core Entry Points**
    *   `main.py`: The command-line interface (CLI) and interactive management console. Handles command parsing, argument dispatch, and provides real-time log tailing with console management.
    *   `settings.py`: The comprehensive configuration file. Defines default values, file paths, external dependency configurations, and configuration templates for Nginx and Hypercorn.

*   **`src/local/` - Process & System Management**
    *   `manager.py` (`ProcessManager`): The application's orchestration hub. Manages the complete lifecycle (start, stop, supervise, restart) of all subprocesses including Nginx, Hypercorn ASGI server, content converter, and optional services like Loki and Ngrok. Handles graceful shutdowns, health checks, and automatic recovery.
    *   `supervisor_entry.py`: A lightweight detached supervisor process that monitors critical processes and provides automatic restart capabilities with cooldown periods and attempt limits.
    *   `app_process.py`: Cross-platform utilities for subprocess creation and management, including platform-specific process flags, log output capture, and executable path resolution.
    *   `config.py`: Implements the hierarchical configuration system that merges `settings.py` defaults, `.env` overrides, and runtime `overrides.json` modifications with proper precedence handling.
    *   `database.py` (`LogDBManager`, `ContentDBManager`): Thread-safe data access layer for SQLite operations. Provides APIs for log storage/retrieval and content database management with WAL mode support for concurrent access.
    *   `externals.py` (`DependencyManager`): Automated dependency management system that handles download, installation, version checking, updates, and recovery of external binaries from GitHub releases and other sources.

*   **`src/web/` - Web Application Logic**
    *   `server.py`: Production-ready Starlette ASGI application with comprehensive middleware stack including DDoS protection, security headers, rate limiting, and asset serving. Handles HTTP requests, database queries, and response generation.
    *   `process.py`: Real-time content processing engine using `watchdog` for filesystem monitoring. Implements multiprocessing pools for parallel content conversion, FFmpeg media optimization, YAML front-matter parsing, and template rendering.

*   **`src/log/` - Observability Infrastructure**
    *   `setup.py`: Centralized logging configuration that sets up structured logging with multiple handlers, log levels, and output formats for both console and persistent storage.
    *   `handler.py` (`SQLiteHandler`, `LokiHandler`): Production-grade custom logging handlers with batching, thread safety, automatic buffering, and integration with Grafana Loki for centralized log aggregation.
    *   `export.py`: Advanced log analysis and reporting system that exports SQLite log data to styled Excel reports with conditional formatting, charts, and filtering capabilities.

*   **`src/templates/` - Dynamic HTML Generation**
    *   `default.py` (`DefaultTemplate`): A feature-rich, responsive HTML template with modern CSS Grid/Flexbox layouts, dark/light mode support, interactive navigation, and mobile-first design. Supports extensive customization through YAML front-matter context variables.
