use anyhow::{bail, Context, Result};
use clap::{Parser, Subcommand};
use globset::{Glob, GlobSet, GlobSetBuilder};
use scenetrace_core::{compare_with_noise, Comparison, NoiseEstimate, NoiseProfile, Run};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::{
    collections::{BTreeMap, VecDeque},
    env,
    fs::{self, File, OpenOptions},
    io::{Read, Write},
    path::{Path, PathBuf},
    process::{Command, ExitCode, Stdio},
    sync::{
        atomic::{AtomicUsize, Ordering},
        Arc, Mutex,
    },
    thread,
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

#[derive(Parser)]
#[command(
    name = "scenetrace",
    version,
    about = "Performance regression testing for Blender scenes"
)]
struct Cli {
    #[command(subcommand)]
    command: Commands,
}

#[derive(Subcommand)]
enum Commands {
    /// Compare a SceneTrace baseline with the latest benchmark.
    Compare {
        /// .scenetrace directory, baseline.json, or raw baseline run JSON.
        input: PathBuf,
        /// Optional latest.json/raw current run JSON. Omit when input is a directory.
        current: Option<PathBuf>,
        #[arg(long, default_value_t = 20.0)]
        threshold_percent: f64,
        #[arg(long, default_value_t = 2.0)]
        min_delta_ms: f64,
        #[arg(long)]
        json: bool,
    },

    /// Create calibrated background-mode baseline(s) for a .blend file or project directory.
    Baseline {
        target: PathBuf,
        /// Explicit Blender executable. Otherwise BLENDER_PATH, PATH, and common install folders are checked.
        #[arg(long)]
        blender_path: Option<PathBuf>,
        #[arg(long)]
        frame_start: Option<i32>,
        #[arg(long)]
        frame_end: Option<i32>,
        #[arg(long)]
        frame_step: Option<i32>,
        #[arg(long)]
        repetitions: Option<usize>,
        #[arg(long)]
        warmups: Option<usize>,
        #[arg(long)]
        calibration_runs: Option<usize>,
        #[arg(long)]
        no_modifier_timings: bool,
        /// Maximum concurrent Blender processes in project mode.
        #[arg(long)]
        workers: Option<usize>,
        #[arg(long, default_value_t = 900)]
        timeout_seconds: u64,
        /// Emit a machine-readable project summary when target is a directory.
        #[arg(long)]
        json: bool,
    },

    /// Benchmark a .blend or every configured asset in a project directory.
    Test {
        target: PathBuf,
        #[arg(long)]
        blender_path: Option<PathBuf>,
        #[arg(long)]
        frame_start: Option<i32>,
        #[arg(long)]
        frame_end: Option<i32>,
        #[arg(long)]
        frame_step: Option<i32>,
        #[arg(long)]
        repetitions: Option<usize>,
        #[arg(long)]
        warmups: Option<usize>,
        #[arg(long)]
        no_modifier_timings: bool,
        #[arg(long)]
        threshold_percent: Option<f64>,
        #[arg(long)]
        min_delta_ms: Option<f64>,
        /// Only benchmark .blend files whose own file bytes changed since their last passing project test.
        #[arg(long)]
        changed: bool,
        /// Maximum concurrent Blender processes in project mode.
        #[arg(long)]
        workers: Option<usize>,
        #[arg(long, default_value_t = 900)]
        timeout_seconds: u64,
        #[arg(long)]
        json: bool,
        /// Write a Markdown project report to this path.
        #[arg(long)]
        markdown: Option<PathBuf>,
        /// Append a Markdown report to GITHUB_STEP_SUMMARY.
        #[arg(long)]
        github_summary: bool,
    },
}

#[derive(Debug)]
struct LoadedBaseline {
    run: Run,
    noise: NoiseProfile,
    frame_noise: BTreeMap<i32, NoiseEstimate>,
    calibrated: bool,
    calibration_runs: usize,
    quality: String,
}

#[derive(Debug, Default, Clone, Deserialize)]
struct ProjectConfig {
    #[serde(default)]
    project: ProjectSection,
    #[serde(default)]
    frames: FrameConfig,
    #[serde(default)]
    benchmark: BenchmarkConfig,
    #[serde(default)]
    budget: BudgetConfig,
    #[serde(default)]
    workers: WorkerConfig,
}

#[derive(Debug, Default, Clone, Deserialize)]
struct ProjectSection {
    #[serde(default)]
    include: Vec<String>,
    #[serde(default)]
    exclude: Vec<String>,
}

#[derive(Debug, Default, Clone, Deserialize)]
struct FrameConfig {
    start: Option<i32>,
    end: Option<i32>,
    step: Option<i32>,
}

#[derive(Debug, Default, Clone, Deserialize)]
struct BenchmarkConfig {
    warmups: Option<usize>,
    repetitions: Option<usize>,
    calibration_runs: Option<usize>,
    capture_modifier_timings: Option<bool>,
}

#[derive(Debug, Default, Clone, Deserialize)]
struct BudgetConfig {
    regression_percent: Option<f64>,
    min_delta_ms: Option<f64>,
    #[allow(dead_code)]
    target_fps: Option<u32>,
}

#[derive(Debug, Default, Clone, Deserialize)]
struct WorkerConfig {
    max_parallel: Option<usize>,
    timeout_seconds: Option<u64>,
}

#[derive(Debug, Clone)]
struct BenchSettings {
    frame_start: Option<i32>,
    frame_end: Option<i32>,
    frame_step: i32,
    repetitions: usize,
    warmups: usize,
    calibration_runs: usize,
    capture_modifier_timings: bool,
}

#[derive(Debug, Default, Clone)]
struct BenchDefaults {
    frame_start: Option<i32>,
    frame_end: Option<i32>,
    frame_step: Option<i32>,
    repetitions: Option<usize>,
    warmups: Option<usize>,
    capture_modifier_timings: Option<bool>,
}

#[derive(Debug, Clone)]
struct BenchOverrides {
    frame_start: Option<i32>,
    frame_end: Option<i32>,
    frame_step: Option<i32>,
    repetitions: Option<usize>,
    warmups: Option<usize>,
    calibration_runs: Option<usize>,
    no_modifier_timings: bool,
}

struct FileBaselineOperation<'a> {
    blend: &'a Path,
    trace_dir: &'a Path,
    blender: &'a Path,
    script: &'a Path,
    settings: &'a BenchSettings,
    timeout_seconds: u64,
}

struct ProjectRunContext<'a> {
    blender: &'a Path,
    script: &'a Path,
    config: &'a ProjectConfig,
    overrides: &'a BenchOverrides,
    timeout_seconds: u64,
}

impl FileBaselineOperation<'_> {
    fn output_path(&self) -> PathBuf {
        self.trace_dir.join("headless-baseline.json")
    }

    fn run(&self) -> Result<LoadedBaseline> {
        fs::create_dir_all(self.trace_dir)?;
        let baseline_path = self.output_path();
        let log_path = self.trace_dir.join("headless.log");
        let mut args = base_runner_args("baseline", &baseline_path, self.settings);
        args.extend([
            "--calibration-runs".into(),
            self.settings.calibration_runs.to_string(),
        ]);
        run_blender(
            self.blender,
            self.blend,
            self.script,
            &args,
            &log_path,
            self.timeout_seconds,
        )?;
        if !baseline_path.is_file() {
            bail!(
                "Blender exited successfully but did not create {}",
                baseline_path.display()
            );
        }
        load_baseline(&baseline_path)
    }
}

#[derive(Debug, Clone)]
struct AssetJob {
    blend: PathBuf,
    relative: String,
    file_hash: String,
    trace_dir: PathBuf,
}

#[derive(Debug, Clone, Serialize)]
struct AssetResult {
    asset: String,
    status: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    p95_ms: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    p95_delta_percent: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pattern: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    confidence: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    likely_source: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    error: Option<String>,
    file_hash: String,
}

impl AssetResult {
    fn error(job: &AssetJob, error: impl ToString) -> Self {
        Self {
            asset: job.relative.clone(),
            status: "ERROR".into(),
            p95_ms: None,
            p95_delta_percent: None,
            pattern: None,
            confidence: None,
            likely_source: None,
            error: Some(error.to_string()),
            file_hash: job.file_hash.clone(),
        }
    }

    fn skipped(job: &AssetJob) -> Self {
        Self {
            asset: job.relative.clone(),
            status: "SKIP".into(),
            p95_ms: None,
            p95_delta_percent: None,
            pattern: None,
            confidence: Some("CACHED".into()),
            likely_source: None,
            error: None,
            file_hash: job.file_hash.clone(),
        }
    }
}

const PROJECT_CACHE_SCHEMA: &str = "scenetrace-project-cache";
const PROJECT_CACHE_SCHEMA_VERSION: u32 = 1;
static TEMP_FILE_SEQUENCE: AtomicUsize = AtomicUsize::new(0);

#[derive(Debug, Clone, Serialize, Deserialize)]
struct ProjectCache {
    #[serde(default)]
    schema: String,
    #[serde(default)]
    schema_version: u32,
    #[serde(default)]
    scenetrace_version: String,
    #[serde(default = "cache_version")]
    version: u32,
    #[serde(default)]
    assets: BTreeMap<String, CacheEntry>,
}

impl Default for ProjectCache {
    fn default() -> Self {
        Self {
            schema: PROJECT_CACHE_SCHEMA.into(),
            schema_version: PROJECT_CACHE_SCHEMA_VERSION,
            scenetrace_version: env!("CARGO_PKG_VERSION").into(),
            version: cache_version(),
            assets: BTreeMap::new(),
        }
    }
}

fn cache_version() -> u32 {
    1
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
struct CacheEntry {
    #[serde(default)]
    baseline_hash: Option<String>,
    #[serde(default)]
    last_success_hash: Option<String>,
    #[serde(default)]
    last_result: Option<String>,
    #[serde(default)]
    p95_ms: Option<f64>,
}

#[derive(Debug, Serialize)]
struct ProjectReport {
    schema: String,
    schema_version: u32,
    scenetrace_version: String,
    created_at_unix_ms: u128,
    root: String,
    mode: String,
    workers: usize,
    changed_only: bool,
    discovered: usize,
    tested: usize,
    passed: usize,
    failed: usize,
    skipped: usize,
    errors: usize,
    assets: Vec<AssetResult>,
}

fn read_json(path: &Path) -> Result<Value> {
    let data = fs::read_to_string(path).with_context(|| format!("reading {}", path.display()))?;
    serde_json::from_str(&data).with_context(|| format!("parsing {}", path.display()))
}

fn run_from_value(value: &Value, wrapper_key: Option<&str>, label: &str) -> Result<Run> {
    let run_value = if let Some(key) = wrapper_key {
        value
            .get(key)
            .cloned()
            .with_context(|| format!("{label} does not contain '{key}'"))?
    } else {
        value.clone()
    };
    serde_json::from_value(run_value)
        .with_context(|| format!("decoding benchmark run from {label}"))
}

fn load_baseline(path: &Path) -> Result<LoadedBaseline> {
    let value = read_json(path)?;
    let schema = value.get("schema").and_then(Value::as_str);
    if let Some(schema) = schema {
        if schema != "scenetrace-baseline" {
            bail!(
                "unsupported baseline schema '{schema}' in {}; existing data was preserved",
                path.display()
            );
        }
        let schema_version = value
            .get("schema_version")
            .and_then(Value::as_u64)
            .unwrap_or(0);
        if schema_version > 1 {
            bail!(
                "baseline {} uses newer schema version {schema_version}; SceneTrace supports up to 1; existing data was preserved",
                path.display()
            );
        }
    }
    let is_profile = schema == Some("scenetrace-baseline") && value.get("aggregate").is_some();

    if !is_profile {
        return Ok(LoadedBaseline {
            run: run_from_value(&value, None, &path.display().to_string())?,
            noise: NoiseProfile::default(),
            frame_noise: BTreeMap::new(),
            calibrated: false,
            calibration_runs: 0,
            quality: "UNKNOWN".into(),
        });
    }

    let run = run_from_value(&value, Some("aggregate"), &path.display().to_string())?;
    let noise: NoiseProfile = value
        .get("noise")
        .cloned()
        .map(serde_json::from_value)
        .transpose()
        .context("decoding calibrated baseline noise")?
        .unwrap_or_default();

    let mut frame_noise = BTreeMap::new();
    if let Some(items) = value.get("frame_noise").and_then(Value::as_object) {
        for (frame, estimate) in items {
            let Ok(frame_number) = frame.parse::<i32>() else {
                continue;
            };
            let parsed: NoiseEstimate = serde_json::from_value(estimate.clone())
                .with_context(|| format!("decoding noise estimate for frame {frame}"))?;
            frame_noise.insert(frame_number, parsed);
        }
    }

    let calibration_runs = value
        .get("calibration_runs")
        .and_then(Value::as_u64)
        .unwrap_or(0) as usize;
    let quality = if noise.quality.is_empty() {
        "UNKNOWN".to_string()
    } else {
        noise.quality.clone()
    };

    Ok(LoadedBaseline {
        run,
        noise,
        frame_noise,
        calibrated: true,
        calibration_runs,
        quality,
    })
}

fn load_current(path: &Path) -> Result<Run> {
    let value = read_json(path)?;
    if value.get("run").is_some() {
        run_from_value(&value, Some("run"), &path.display().to_string())
    } else {
        run_from_value(&value, None, &path.display().to_string())
    }
}

fn resolve_paths(
    input: PathBuf,
    current: Option<PathBuf>,
) -> Result<(PathBuf, PathBuf, Option<PathBuf>)> {
    if input.is_dir() {
        if current.is_some() {
            bail!("when input is a .scenetrace directory, do not pass a second file");
        }
        let baseline = input.join("baseline.json");
        let latest = input.join("latest.json");
        if !baseline.is_file() {
            bail!("{} does not contain baseline.json", input.display());
        }
        if !latest.is_file() {
            bail!("{} does not contain latest.json", input.display());
        }
        return Ok((baseline, latest, Some(input)));
    }

    if let Some(current) = current {
        return Ok((input, current, None));
    }

    if input.file_name().and_then(|name| name.to_str()) == Some("baseline.json") {
        let latest = input.with_file_name("latest.json");
        if latest.is_file() {
            return Ok((input, latest, None));
        }
        bail!("no sibling latest.json found next to {}", input.display());
    }

    bail!("pass a .scenetrace directory, or provide both baseline and current JSON files")
}

fn absolute_existing(path: PathBuf) -> Result<PathBuf> {
    let path = if path.is_absolute() {
        path
    } else {
        env::current_dir()
            .context("reading current directory")?
            .join(path)
    };
    if !path.exists() {
        bail!("path does not exist: {}", path.display());
    }
    Ok(fs::canonicalize(&path).unwrap_or(path))
}

fn resolve_blend_path(path: PathBuf) -> Result<PathBuf> {
    let path = absolute_existing(path)?;
    if !path.is_file() {
        bail!(".blend file does not exist: {}", path.display());
    }
    if path.extension().and_then(|v| v.to_str()) != Some("blend") {
        bail!("expected a .blend file: {}", path.display());
    }
    Ok(path)
}

fn trace_dir_for_blend(blend: &Path) -> Result<PathBuf> {
    let parent = blend
        .parent()
        .with_context(|| format!("cannot determine project directory for {}", blend.display()))?;
    Ok(parent.join(".scenetrace"))
}

fn load_project_config(trace_dir: &Path) -> Result<ProjectConfig> {
    let path = trace_dir.join("config.json");
    if !path.is_file() {
        return Ok(ProjectConfig::default());
    }
    let text = fs::read_to_string(&path).with_context(|| format!("reading {}", path.display()))?;
    serde_json::from_str(&text).with_context(|| format!("parsing {}", path.display()))
}

#[allow(clippy::too_many_arguments)]
fn resolve_bench_settings(
    config: &ProjectConfig,
    defaults: Option<&BenchDefaults>,
    frame_start: Option<i32>,
    frame_end: Option<i32>,
    frame_step: Option<i32>,
    repetitions: Option<usize>,
    warmups: Option<usize>,
    calibration_runs: Option<usize>,
    no_modifier_timings: bool,
) -> BenchSettings {
    let defaults = defaults.cloned().unwrap_or_default();
    BenchSettings {
        frame_start: frame_start.or(config.frames.start).or(defaults.frame_start),
        frame_end: frame_end.or(config.frames.end).or(defaults.frame_end),
        frame_step: frame_step
            .or(config.frames.step)
            .or(defaults.frame_step)
            .unwrap_or(1)
            .max(1),
        repetitions: repetitions
            .or(config.benchmark.repetitions)
            .or(defaults.repetitions)
            .unwrap_or(3)
            .max(1),
        warmups: warmups
            .or(config.benchmark.warmups)
            .or(defaults.warmups)
            .unwrap_or(1),
        calibration_runs: calibration_runs
            .or(config.benchmark.calibration_runs)
            .unwrap_or(4)
            .max(1),
        capture_modifier_timings: if no_modifier_timings {
            false
        } else {
            config
                .benchmark
                .capture_modifier_timings
                .or(defaults.capture_modifier_timings)
                .unwrap_or(true)
        },
    }
}

fn resolve_with_overrides(
    config: &ProjectConfig,
    defaults: Option<&BenchDefaults>,
    overrides: &BenchOverrides,
) -> BenchSettings {
    resolve_bench_settings(
        config,
        defaults,
        overrides.frame_start,
        overrides.frame_end,
        overrides.frame_step,
        overrides.repetitions,
        overrides.warmups,
        overrides.calibration_runs,
        overrides.no_modifier_timings,
    )
}

fn load_benchmark_defaults(path: &Path) -> Result<BenchDefaults> {
    let value = read_json(path)?;
    let settings = value
        .get("aggregate")
        .and_then(|v| v.get("settings"))
        .or_else(|| value.get("settings"));
    let Some(settings) = settings else {
        return Ok(BenchDefaults::default());
    };
    Ok(BenchDefaults {
        frame_start: settings
            .get("frame_start")
            .and_then(Value::as_i64)
            .map(|v| v as i32),
        frame_end: settings
            .get("frame_end")
            .and_then(Value::as_i64)
            .map(|v| v as i32),
        frame_step: settings
            .get("frame_step")
            .and_then(Value::as_i64)
            .map(|v| v as i32),
        repetitions: settings
            .get("repetitions")
            .and_then(Value::as_u64)
            .map(|v| v as usize),
        warmups: settings
            .get("warmups")
            .and_then(Value::as_u64)
            .map(|v| v as usize),
        capture_modifier_timings: settings
            .get("capture_modifier_timings")
            .and_then(Value::as_bool),
    })
}

fn command_available(program: &Path) -> bool {
    Command::new(program)
        .arg("--version")
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .map(|s| s.success())
        .unwrap_or(false)
}

fn discover_blender(explicit: Option<PathBuf>) -> Result<PathBuf> {
    if let Some(path) = explicit {
        if command_available(&path) {
            return Ok(path);
        }
        bail!("Blender executable is not runnable: {}", path.display());
    }

    if let Ok(value) = env::var("BLENDER_PATH") {
        let path = PathBuf::from(value);
        if command_available(&path) {
            return Ok(path);
        }
    }

    let path_command = PathBuf::from(if cfg!(windows) {
        "blender.exe"
    } else {
        "blender"
    });
    if command_available(&path_command) {
        return Ok(path_command);
    }

    #[cfg(windows)]
    {
        let mut candidates = Vec::new();
        for variable in ["ProgramFiles", "ProgramW6432"] {
            if let Ok(root) = env::var(variable) {
                let foundation = PathBuf::from(root).join("Blender Foundation");
                if let Ok(entries) = fs::read_dir(foundation) {
                    for entry in entries.flatten() {
                        let candidate = entry.path().join("blender.exe");
                        if candidate.is_file() {
                            candidates.push(candidate);
                        }
                    }
                }
            }
        }
        candidates.sort_by(|a, b| b.to_string_lossy().cmp(&a.to_string_lossy()));
        if let Some(path) = candidates.into_iter().find(|p| command_available(p)) {
            return Ok(path);
        }
    }

    bail!(
        "Blender was not found. Put blender on PATH, set BLENDER_PATH, or pass --blender-path <path>"
    )
}

fn headless_script_path() -> Result<PathBuf> {
    if let Ok(value) = env::var("SCENETRACE_HEADLESS_SCRIPT") {
        let path = PathBuf::from(value);
        if path.is_file() {
            return Ok(path);
        }
    }

    let manifest = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    if let Some(root) = manifest.parent().and_then(Path::parent) {
        let path = root.join("blender").join("scenetrace").join("headless.py");
        if path.is_file() {
            return Ok(path);
        }
    }

    if let Ok(exe) = env::current_exe() {
        if let Some(dir) = exe.parent() {
            for path in [
                dir.join("headless.py"),
                dir.join("share").join("scenetrace").join("headless.py"),
            ] {
                if path.is_file() {
                    return Ok(path);
                }
            }
        }
    }

    bail!(
        "SceneTrace headless runner was not found. Run from the source checkout or set SCENETRACE_HEADLESS_SCRIPT"
    )
}

fn tail_file(path: &Path, lines: usize) -> String {
    let mut data = String::new();
    if File::open(path)
        .and_then(|mut f| f.read_to_string(&mut data))
        .is_err()
    {
        return String::new();
    }
    let all: Vec<_> = data.lines().collect();
    all[all.len().saturating_sub(lines)..].join("\n")
}

fn blender_command(blender: &Path, blend: &Path, script: &Path, runner_args: &[String]) -> Command {
    let mut command = Command::new(blender);
    command
        .arg("--factory-startup")
        .arg("--background")
        .arg(blend)
        .arg("--python")
        .arg(script)
        .arg("--")
        .args(runner_args);
    command
}

fn run_blender(
    blender: &Path,
    blend: &Path,
    script: &Path,
    runner_args: &[String],
    log_path: &Path,
    timeout_seconds: u64,
) -> Result<()> {
    if !blend.is_file() {
        bail!(".blend file does not exist: {}", blend.display());
    }
    if let Some(parent) = log_path.parent() {
        fs::create_dir_all(parent)?;
    }
    let log = File::create(log_path).with_context(|| format!("creating {}", log_path.display()))?;
    let log_err = log.try_clone()?;

    let mut child = blender_command(blender, blend, script, runner_args)
        .stdout(Stdio::from(log))
        .stderr(Stdio::from(log_err))
        .spawn()
        .with_context(|| format!("launching Blender at {}", blender.display()))?;

    let started = Instant::now();
    let timeout = Duration::from_secs(timeout_seconds.max(1));
    loop {
        if let Some(status) = child.try_wait().context("checking Blender process")? {
            if status.success() {
                return Ok(());
            }
            let tail = tail_file(log_path, 30);
            bail!(
                "Blender headless benchmark failed with {status}. Log: {}\n{}",
                log_path.display(),
                tail
            );
        }
        if started.elapsed() >= timeout {
            let _ = child.kill();
            let _ = child.wait();
            let tail = tail_file(log_path, 30);
            bail!(
                "Blender benchmark timed out after {}s. Log: {}\n{}",
                timeout_seconds,
                log_path.display(),
                tail
            );
        }
        thread::sleep(Duration::from_millis(100));
    }
}

fn push_option<T: ToString>(args: &mut Vec<String>, name: &str, value: Option<T>) {
    if let Some(value) = value {
        args.push(name.to_string());
        args.push(value.to_string());
    }
}

fn base_runner_args(command: &str, output: &Path, settings: &BenchSettings) -> Vec<String> {
    let mut args = vec![command.to_string()];
    push_option(&mut args, "--frame-start", settings.frame_start);
    push_option(&mut args, "--frame-end", settings.frame_end);
    args.extend(["--frame-step".into(), settings.frame_step.to_string()]);
    args.extend(["--repetitions".into(), settings.repetitions.to_string()]);
    args.extend(["--warmups".into(), settings.warmups.to_string()]);
    if !settings.capture_modifier_timings {
        args.push("--no-modifier-timings".into());
    }
    args.extend(["--output".into(), output.display().to_string()]);
    args
}

fn diagnosis_summary(path: &Path) -> Option<Value> {
    let value = read_json(path).ok()?;
    value
        .get("comparison")?
        .get("diagnosis")?
        .get("summary")
        .cloned()
}

fn likely_source_from_diagnosis(diagnosis: Option<&Value>) -> Option<String> {
    let summary = diagnosis?;
    let object = summary.get("object").and_then(Value::as_str);
    let modifier = summary.get("modifier").and_then(Value::as_str);
    let headline = summary.get("headline").and_then(Value::as_str);
    match (modifier, object, headline) {
        (Some(modifier), Some(object), _) => Some(format!("{modifier} on {object}")),
        (_, _, Some(headline)) => Some(headline.to_string()),
        (_, Some(object), _) => Some(object.to_string()),
        _ => None,
    }
}

fn compute_comparison(
    baseline_path: &Path,
    current_path: &Path,
    threshold_percent: f64,
    min_delta_ms: f64,
) -> Result<(LoadedBaseline, Run, Comparison, Option<Value>)> {
    let base = load_baseline(baseline_path)?;
    let cur = load_current(current_path)?;
    let result = compare_with_noise(
        &base.run,
        &cur,
        threshold_percent,
        min_delta_ms,
        &base.noise,
        &base.frame_noise,
    );
    let diagnosis = diagnosis_summary(current_path);
    Ok((base, cur, result, diagnosis))
}

fn environment_incompatibility(path: &Path) -> Result<Option<String>> {
    let value = read_json(path)?;
    let incompatible = value
        .get("comparison")
        .and_then(|details| details.get("classification"))
        .and_then(Value::as_str)
        == Some("environment_incompatible");
    if !incompatible {
        return Ok(None);
    }
    let reason = value
        .get("comparison")
        .and_then(|details| details.get("environment"))
        .and_then(|environment| environment.get("reasons"))
        .and_then(Value::as_array)
        .map(|reasons| {
            reasons
                .iter()
                .filter_map(Value::as_str)
                .collect::<Vec<_>>()
                .join("; ")
        })
        .filter(|reason| !reason.is_empty())
        .unwrap_or_else(|| "baseline environment is incompatible with this measurement".into());
    Ok(Some(reason))
}

fn headless_result_status(
    path: &Path,
    comparison: &Comparison,
) -> Result<(String, Option<String>)> {
    if let Some(reason) = environment_incompatibility(path)? {
        return Ok(("ENVIRONMENT_INCOMPATIBLE".into(), Some(reason)));
    }
    Ok((if comparison.failed { "FAIL" } else { "PASS" }.into(), None))
}

fn execute_compare(
    baseline_path: &Path,
    current_path: &Path,
    source_label: Option<&Path>,
    threshold_percent: f64,
    min_delta_ms: f64,
    json: bool,
    title: &str,
) -> Result<bool> {
    let (base, cur, result, diagnosis) =
        compute_comparison(baseline_path, current_path, threshold_percent, min_delta_ms)?;

    if json {
        let output = serde_json::json!({
            "schema": "scenetrace-cli-comparison",
            "schema_version": 1,
            "scenetrace_version": env!("CARGO_PKG_VERSION"),
            "source": source_label.map(|p| p.display().to_string()),
            "baseline_path": baseline_path.display().to_string(),
            "current_path": current_path.display().to_string(),
            "baseline": {
                "calibrated": base.calibrated,
                "calibration_runs": base.calibration_runs,
                "quality": base.quality,
                "p95_ms": base.run.summary.p95_ms,
                "p95_noise_percent": base.noise.p95.percent,
                "p95_noise_ms": base.noise.p95.ms,
            },
            "current": {
                "p95_ms": cur.summary.p95_ms,
                "median_ms": cur.summary.median_ms,
                "worst_ms": cur.summary.worst_ms,
                "worst_frame": cur.summary.worst_frame,
            },
            "comparison": &result,
            "diagnosis": diagnosis,
        });
        println!("{}", serde_json::to_string_pretty(&output)?);
        return Ok(result.failed);
    }

    println!("{title}");
    if let Some(source) = source_label {
        println!("Source:   {}", source.display());
    }
    println!();
    println!(
        "Median:  {:>8.2} -> {:>8.2} ms  ({:+.1}%)",
        base.run.summary.median_ms, cur.summary.median_ms, result.median_delta_percent
    );
    println!(
        "P95:     {:>8.2} -> {:>8.2} ms  ({:+.1}%)",
        base.run.summary.p95_ms, cur.summary.p95_ms, result.p95_delta_percent
    );
    println!(
        "Worst:   {:>8.2} -> {:>8.2} ms  ({:+.1}%)",
        base.run.summary.worst_ms, cur.summary.worst_ms, result.worst_delta_percent
    );
    println!();

    if base.calibrated {
        println!(
            "Baseline: calibrated · {} runs · {}",
            base.calibration_runs, base.quality
        );
        println!(
            "P95 noise: +/-{:.1}% ({:.2} ms)",
            base.noise.p95.percent, base.noise.p95.ms
        );
    } else {
        println!("Baseline: single/raw run · noise floor unknown");
    }
    println!(
        "Effective P95 gate: +{:.1}% and +{:.2} ms",
        result.effective_p95_threshold_percent, result.effective_p95_threshold_ms
    );
    println!(
        "Pattern:  {} · {}/{} frames ({:.1}%)",
        result.pattern.kind.to_uppercase(),
        result.pattern.affected_frames,
        result.pattern.total_frames,
        result.pattern.affected_percent
    );
    println!("Confidence: {}", result.confidence);

    if let Some(summary) = diagnosis.as_ref() {
        let object = summary.get("object").and_then(Value::as_str);
        let modifier = summary.get("modifier").and_then(Value::as_str);
        let headline = summary.get("headline").and_then(Value::as_str);
        let confidence = summary
            .get("confidence")
            .and_then(Value::as_str)
            .unwrap_or("LOW");
        if object.is_some() || headline.is_some() {
            println!();
            println!("Likely contributor ({confidence} confidence):");
            if let (Some(modifier), Some(object)) = (modifier, object) {
                println!("  {modifier} on {object}");
            } else if let Some(headline) = headline {
                println!("  {headline}");
            }
            if let Some(ms) = summary.get("measured_delta_ms").and_then(Value::as_f64) {
                println!("  measured signal: +{ms:.2} ms");
            }
            if let Some(coverage) = summary.get("coverage_percent").and_then(Value::as_f64) {
                println!("  conservative coverage: ~{coverage:.0}%");
            }
        }
    }

    if !result.regressed_frames.is_empty() {
        println!();
        println!("Worst regressed frames:");
        for frame in result.regressed_frames.iter().take(10) {
            println!(
                "  {:>5}: {:>7.2} -> {:>7.2} ms  ({:+.1}%)",
                frame.frame, frame.baseline_ms, frame.current_ms, frame.delta_percent
            );
        }
    }
    println!();
    println!("Result: {}", if result.failed { "FAIL" } else { "PASS" });
    Ok(result.failed)
}

fn normalize_relative(path: &Path) -> String {
    path.to_string_lossy().replace('\\', "/")
}

fn is_scenetrace_metadata_dir(path: &Path) -> bool {
    path.file_name().and_then(|name| name.to_str()) == Some(".scenetrace")
}

fn should_descend_into(path: &Path, file_type: &fs::FileType) -> bool {
    if !file_type.is_dir() || file_type.is_symlink() {
        return false;
    }

    #[cfg(windows)]
    {
        use std::os::windows::fs::MetadataExt;

        const FILE_ATTRIBUTE_REPARSE_POINT: u32 = 0x0400;
        fs::symlink_metadata(path)
            .map(|metadata| metadata.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT == 0)
            .unwrap_or(false)
    }

    #[cfg(not(windows))]
    {
        let _ = path;
        true
    }
}

fn build_globset(patterns: &[String], defaults: &[&str]) -> Result<GlobSet> {
    let mut builder = GlobSetBuilder::new();
    if patterns.is_empty() {
        for pattern in defaults {
            builder.add(Glob::new(pattern).with_context(|| format!("invalid glob {pattern}"))?);
        }
    } else {
        for pattern in patterns {
            builder.add(Glob::new(pattern).with_context(|| format!("invalid glob {pattern}"))?);
        }
    }
    builder.build().context("building project glob matcher")
}

fn discover_assets(root: &Path, config: &ProjectConfig) -> Result<Vec<PathBuf>> {
    let includes = build_globset(&config.project.include, &["*.blend", "**/*.blend"])?;
    let excludes = build_globset(&config.project.exclude, &["**/.scenetrace/**"])?;
    let mut assets = Vec::new();
    let mut pending = vec![root.to_path_buf()];

    while let Some(dir) = pending.pop() {
        let entries = match fs::read_dir(&dir) {
            Ok(entries) => entries,
            Err(error) => {
                eprintln!(
                    "Warning: skipping unreadable directory {}: {error}",
                    dir.display()
                );
                continue;
            }
        };
        for entry in entries {
            let entry = match entry {
                Ok(entry) => entry,
                Err(error) => {
                    eprintln!(
                        "Warning: skipping unreadable entry in {}: {error}",
                        dir.display()
                    );
                    continue;
                }
            };
            let path = entry.path();
            let relative = path.strip_prefix(root).unwrap_or(&path);
            let normalized = normalize_relative(relative);
            let file_type = match entry.file_type() {
                Ok(file_type) => file_type,
                Err(error) => {
                    eprintln!(
                        "Warning: skipping unreadable entry {}: {error}",
                        path.display()
                    );
                    continue;
                }
            };
            if should_descend_into(&path, &file_type) {
                if is_scenetrace_metadata_dir(&path) {
                    continue;
                }
                if !excludes.is_match(&normalized) {
                    pending.push(path);
                }
            } else if file_type.is_file()
                && path.extension().and_then(|v| v.to_str()) == Some("blend")
                && includes.is_match(&normalized)
                && !excludes.is_match(&normalized)
            {
                assets.push(path);
            }
        }
    }

    assets.sort_by_key(|path| normalize_relative(path.strip_prefix(root).unwrap_or(path)));
    Ok(assets)
}

fn hash_file(path: &Path) -> Result<String> {
    let mut file =
        File::open(path).with_context(|| format!("opening {} for hashing", path.display()))?;
    let mut hasher = blake3::Hasher::new();
    let mut buffer = vec![0u8; 1024 * 1024];
    loop {
        let read = file.read(&mut buffer)?;
        if read == 0 {
            break;
        }
        hasher.update(&buffer[..read]);
    }
    Ok(hasher.finalize().to_hex().to_string())
}

fn recorded_dependency_paths(trace_dir: &Path) -> Result<Vec<PathBuf>> {
    let baseline_path = trace_dir.join("headless-baseline.json");
    if !baseline_path.is_file() {
        return Ok(Vec::new());
    }
    let value = read_json(&baseline_path).with_context(|| {
        format!(
            "reading dependency manifest from {}",
            baseline_path.display()
        )
    })?;
    let mut dependencies: Vec<PathBuf> = value
        .get("dependencies")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(|item| item.get("path").and_then(Value::as_str))
        .map(PathBuf::from)
        .collect();
    dependencies.sort();
    dependencies.dedup();
    Ok(dependencies)
}

fn asset_fingerprint(blend: &Path, trace_dir: &Path) -> Result<String> {
    let mut hasher = blake3::Hasher::new();
    hasher.update(b"scene\0");
    hasher.update(hash_file(blend)?.as_bytes());
    for dependency in recorded_dependency_paths(trace_dir)? {
        hasher.update(b"\0dependency\0");
        hasher.update(dependency.as_os_str().to_string_lossy().as_bytes());
        if dependency.is_file() {
            hasher.update(b"\0present\0");
            hasher.update(
                hash_file(&dependency)
                    .with_context(|| {
                        format!("hashing recorded dependency {}", dependency.display())
                    })?
                    .as_bytes(),
            );
        } else {
            hasher.update(b"\0missing\0");
        }
    }
    Ok(hasher.finalize().to_hex().to_string())
}

fn asset_storage_key(relative: &str) -> String {
    let stem = Path::new(relative)
        .file_stem()
        .and_then(|v| v.to_str())
        .unwrap_or("asset");
    let safe: String = stem
        .chars()
        .map(|ch| {
            if ch.is_ascii_alphanumeric() || ch == '-' || ch == '_' {
                ch
            } else {
                '_'
            }
        })
        .take(36)
        .collect();
    let digest = blake3::hash(relative.as_bytes()).to_hex().to_string();
    let prefix = if safe.is_empty() {
        "asset"
    } else {
        safe.as_str()
    };
    format!("{prefix}-{}", &digest[..10])
}

fn project_asset_jobs(
    root: &Path,
    trace_dir: &Path,
    config: &ProjectConfig,
) -> Result<Vec<AssetJob>> {
    let assets = discover_assets(root, config)?;
    let mut jobs = Vec::with_capacity(assets.len());
    for blend in assets {
        let relative =
            normalize_relative(blend.strip_prefix(root).with_context(|| {
                format!("{} is not inside {}", blend.display(), root.display())
            })?);
        let asset_dir = trace_dir.join("assets").join(asset_storage_key(&relative));
        let file_hash = asset_fingerprint(&blend, &asset_dir)?;
        jobs.push(AssetJob {
            blend,
            relative,
            file_hash,
            trace_dir: asset_dir,
        });
    }
    Ok(jobs)
}

fn load_cache(trace_dir: &Path) -> Result<ProjectCache> {
    let path = trace_dir.join("cache.json");
    if !path.is_file() {
        return Ok(ProjectCache::default());
    }
    let text =
        fs::read_to_string(&path).with_context(|| format!("reading cache {}", path.display()))?;
    let mut cache = match serde_json::from_str::<ProjectCache>(&text) {
        Ok(cache) => cache,
        Err(error) => {
            eprintln!(
                "Warning: ignoring malformed disposable cache {}: {error}. Existing baselines are preserved.",
                path.display()
            );
            return Ok(ProjectCache::default());
        }
    };
    if cache.schema.is_empty() {
        eprintln!(
            "Warning: migrating legacy cache {} to the {} schema.",
            path.display(),
            PROJECT_CACHE_SCHEMA
        );
        cache.schema = PROJECT_CACHE_SCHEMA.into();
        cache.schema_version = PROJECT_CACHE_SCHEMA_VERSION;
    } else if cache.schema != PROJECT_CACHE_SCHEMA {
        bail!(
            "unsupported cache schema '{}' in {}; existing data was preserved",
            cache.schema,
            path.display()
        );
    } else if cache.schema_version > PROJECT_CACHE_SCHEMA_VERSION {
        bail!(
            "cache {} uses newer schema version {}; SceneTrace supports up to {}; existing data was preserved",
            path.display(),
            cache.schema_version,
            PROJECT_CACHE_SCHEMA_VERSION
        );
    }
    cache.scenetrace_version = env!("CARGO_PKG_VERSION").into();
    Ok(cache)
}

fn save_json_atomic<T: Serialize>(path: &Path, value: &T) -> Result<()> {
    let parent = path
        .parent()
        .with_context(|| format!("determining parent directory for {}", path.display()))?;
    fs::create_dir_all(parent)
        .with_context(|| format!("creating metadata directory {}", parent.display()))?;
    let file_name = path
        .file_name()
        .and_then(|name| name.to_str())
        .with_context(|| format!("determining metadata file name for {}", path.display()))?;
    let sequence = TEMP_FILE_SEQUENCE.fetch_add(1, Ordering::Relaxed);
    let temp = parent.join(format!(
        ".{file_name}.{}.{}.tmp",
        std::process::id(),
        sequence
    ));
    let text = serde_json::to_string_pretty(value)? + "\n";
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&temp)
        .with_context(|| format!("creating temporary metadata file {}", temp.display()))?;
    file.write_all(text.as_bytes())
        .with_context(|| format!("writing temporary metadata file {}", temp.display()))?;
    file.sync_all()
        .with_context(|| format!("flushing temporary metadata file {}", temp.display()))?;
    drop(file);

    let mut last_error = None;
    for attempt in 0..5 {
        match fs::rename(&temp, path) {
            Ok(()) => return Ok(()),
            Err(error) => {
                last_error = Some(error);
                thread::sleep(Duration::from_millis(20 * (attempt + 1)));
            }
        }
    }
    let _ = fs::remove_file(&temp);
    bail!(
        "atomically replacing metadata {} failed after retrying Windows file locks; existing data was preserved: {}",
        path.display(),
        last_error
            .map(|error| error.to_string())
            .unwrap_or_else(|| "unknown replacement failure".into())
    )
}

fn save_cache(trace_dir: &Path, cache: &ProjectCache) -> Result<()> {
    let path = trace_dir.join("cache.json");
    if let Err(error) = save_json_atomic(&path, cache) {
        eprintln!(
            "Warning: cache update skipped for {}: {error}. Benchmark results and baselines remain valid.",
            path.display()
        );
    }
    Ok(())
}

fn default_worker_count(
    config: &ProjectConfig,
    cli_workers: Option<usize>,
    asset_count: usize,
) -> usize {
    if asset_count == 0 {
        return 1;
    }
    let available = thread::available_parallelism()
        .map(|v| v.get())
        .unwrap_or(1);
    cli_workers
        .or(config.workers.max_parallel)
        .unwrap_or(available.min(2))
        .max(1)
        .min(asset_count)
}

fn effective_timeout(config: &ProjectConfig, cli_timeout: u64) -> u64 {
    config.workers.timeout_seconds.unwrap_or(cli_timeout).max(1)
}

fn run_parallel<F>(jobs: Vec<AssetJob>, workers: usize, work: F) -> Vec<AssetResult>
where
    F: Fn(AssetJob) -> AssetResult + Sync,
{
    let queue = Arc::new(Mutex::new(VecDeque::from(jobs)));
    let results = Arc::new(Mutex::new(Vec::<AssetResult>::new()));
    thread::scope(|scope| {
        for _ in 0..workers.max(1) {
            let queue = Arc::clone(&queue);
            let results = Arc::clone(&results);
            let work = &work;
            scope.spawn(move || loop {
                let job = {
                    let mut queue = queue.lock().expect("project job queue poisoned");
                    queue.pop_front()
                };
                let Some(job) = job else {
                    break;
                };
                let result = work(job);
                results
                    .lock()
                    .expect("project result queue poisoned")
                    .push(result);
            });
        }
    });
    let mut output = Arc::try_unwrap(results)
        .expect("project result workers still active")
        .into_inner()
        .expect("project result queue poisoned");
    output.sort_by(|a, b| a.asset.cmp(&b.asset));
    output
}

fn run_project_baseline_jobs<F>(
    jobs: Vec<AssetJob>,
    workers: usize,
    baseline_file: F,
) -> Vec<AssetResult>
where
    F: Fn(AssetJob) -> AssetResult + Sync,
{
    run_parallel(jobs, workers, baseline_file)
}

fn project_baseline_asset(job: AssetJob, run: &ProjectRunContext) -> AssetResult {
    let attempt = || -> Result<AssetResult> {
        let settings = resolve_with_overrides(run.config, None, run.overrides);
        let baseline = FileBaselineOperation {
            blend: &job.blend,
            trace_dir: &job.trace_dir,
            blender: run.blender,
            script: run.script,
            settings: &settings,
            timeout_seconds: run.timeout_seconds,
        }
        .run()?;
        let file_hash = asset_fingerprint(&job.blend, &job.trace_dir)?;
        Ok(AssetResult {
            asset: job.relative.clone(),
            status: "BASELINED".into(),
            p95_ms: Some(baseline.run.summary.p95_ms),
            p95_delta_percent: None,
            pattern: None,
            confidence: Some(baseline.quality),
            likely_source: None,
            error: None,
            file_hash,
        })
    };
    attempt().unwrap_or_else(|err| AssetResult::error(&job, format!("{err:#}")))
}

fn project_test_asset(
    job: AssetJob,
    run: &ProjectRunContext,
    threshold_percent: f64,
    min_delta_ms: f64,
) -> AssetResult {
    let attempt = || -> Result<AssetResult> {
        fs::create_dir_all(&job.trace_dir)?;
        let baseline_path = job.trace_dir.join("headless-baseline.json");
        if !baseline_path.is_file() {
            bail!("no project baseline; run `scenetrace baseline <project>` first");
        }
        let defaults = load_benchmark_defaults(&baseline_path)?;
        let settings = resolve_with_overrides(run.config, Some(&defaults), run.overrides);
        let latest_path = job.trace_dir.join("headless-latest.json");
        let log_path = job.trace_dir.join("headless.log");
        let mut args = base_runner_args("test", &latest_path, &settings);
        args.extend([
            "--baseline".into(),
            baseline_path.display().to_string(),
            "--threshold-percent".into(),
            threshold_percent.to_string(),
            "--min-delta-ms".into(),
            min_delta_ms.to_string(),
        ]);
        run_blender(
            run.blender,
            &job.blend,
            run.script,
            &args,
            &log_path,
            run.timeout_seconds,
        )?;
        let (_base, current, comparison, diagnosis) = compute_comparison(
            &baseline_path,
            &latest_path,
            threshold_percent,
            min_delta_ms,
        )?;
        let (status, error) = headless_result_status(&latest_path, &comparison)?;
        Ok(AssetResult {
            asset: job.relative.clone(),
            status,
            p95_ms: Some(current.summary.p95_ms),
            p95_delta_percent: Some(comparison.p95_delta_percent),
            pattern: Some(comparison.pattern.kind.to_uppercase()),
            confidence: Some(comparison.confidence),
            likely_source: likely_source_from_diagnosis(diagnosis.as_ref()),
            error,
            file_hash: job.file_hash.clone(),
        })
    };
    attempt().unwrap_or_else(|err| AssetResult::error(&job, format!("{err:#}")))
}

fn make_project_report(
    root: &Path,
    mode: &str,
    workers: usize,
    changed_only: bool,
    discovered: usize,
    mut assets: Vec<AssetResult>,
) -> ProjectReport {
    assets.sort_by(|a, b| a.asset.cmp(&b.asset));
    let passed = assets.iter().filter(|r| r.status == "PASS").count();
    let failed = assets.iter().filter(|r| r.status == "FAIL").count();
    let skipped = assets.iter().filter(|r| r.status == "SKIP").count();
    let errors = assets
        .iter()
        .filter(|result| {
            !matches!(
                result.status.as_str(),
                "PASS" | "FAIL" | "SKIP" | "BASELINED"
            )
        })
        .count();
    let tested = assets.len().saturating_sub(skipped);
    ProjectReport {
        schema: "scenetrace-project-report".into(),
        schema_version: 1,
        scenetrace_version: env!("CARGO_PKG_VERSION").into(),
        created_at_unix_ms: SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_millis(),
        root: root.display().to_string(),
        mode: mode.into(),
        workers,
        changed_only,
        discovered,
        tested,
        passed,
        failed,
        skipped,
        errors,
        assets,
    }
}

fn print_project_report(report: &ProjectReport, blender: &Path) {
    let title = if report.mode == "baseline" {
        "SceneTrace 1.0 Project Baseline"
    } else {
        "SceneTrace 1.0 Project Test"
    };
    println!("{title}");
    println!("Root:     {}", report.root);
    println!("Blender:  {}", blender.display());
    println!("Workers:  {}", report.workers);
    if report.changed_only {
        println!("Mode:     changed-only (.blend file hash)");
    }
    println!();

    for result in &report.assets {
        match result.status.as_str() {
            "PASS" | "FAIL" => {
                let p95 = result
                    .p95_ms
                    .map(|v| format!("{v:.2} ms"))
                    .unwrap_or("-".into());
                let delta = result
                    .p95_delta_percent
                    .map(|v| format!("{v:+.1}%"))
                    .unwrap_or("-".into());
                println!("{:<8} {}", result.status, result.asset);
                println!("         P95 {p95} · {delta}");
                if let Some(source) = &result.likely_source {
                    println!("         -> {source}");
                }
            }
            "BASELINED" => {
                let p95 = result
                    .p95_ms
                    .map(|v| format!("{v:.2} ms"))
                    .unwrap_or("-".into());
                println!("BASELINE {}", result.asset);
                println!("         P95 {p95}");
            }
            "SKIP" => println!("SKIP     {} · unchanged", result.asset),
            "ERROR" => {
                println!("ERROR    {}", result.asset);
                if let Some(error) = &result.error {
                    println!("         {error}");
                }
            }
            "ENVIRONMENT_INCOMPATIBLE" => {
                println!("INVALID  {}", result.asset);
                if let Some(error) = &result.error {
                    println!("         {error}");
                }
            }
            other => println!("{:<8} {}", other, result.asset),
        }
        println!();
    }

    if report.mode == "test" {
        println!(
            "{} passed · {} regressions · {} cached · {} errors",
            report.passed, report.failed, report.skipped, report.errors
        );
        let result = if report.errors > 0 {
            "ERROR"
        } else if report.failed > 0 {
            "FAIL"
        } else {
            "PASS"
        };
        println!("Result: {result}");
        if report.changed_only {
            println!("Note: --changed fingerprints each .blend file and dependencies reported by its baseline.");
        }
    } else {
        let baselined = report
            .assets
            .iter()
            .filter(|r| r.status == "BASELINED")
            .count();
        println!("{baselined} baselined · {} errors", report.errors);
        println!(
            "Result: {}",
            if report.errors > 0 {
                "ERROR"
            } else {
                "BASELINES SAVED"
            }
        );
    }
}

fn markdown_project_report(report: &ProjectReport) -> String {
    let mut out = String::new();
    out.push_str("### SceneTrace Performance\n\n");
    if report.mode == "baseline" {
        out.push_str(&format!(
            "{} assets baselined, {} errors.\n\n",
            report
                .assets
                .iter()
                .filter(|r| r.status == "BASELINED")
                .count(),
            report.errors
        ));
    } else if report.errors > 0 {
        out.push_str(&format!("⚠️ {} benchmark errors.\n\n", report.errors));
    } else if report.failed > 0 {
        out.push_str(&format!(
            "❌ {} performance regressions detected.\n\n",
            report.failed
        ));
    } else {
        out.push_str("✅ Project performance check passed.\n\n");
    }
    out.push_str("| Asset | Status | P95 | Change | Likely source |\n");
    out.push_str("|---|---|---:|---:|---|\n");
    for result in &report.assets {
        let p95 = result
            .p95_ms
            .map(|v| format!("{v:.2} ms"))
            .unwrap_or("—".into());
        let delta = result
            .p95_delta_percent
            .map(|v| format!("{v:+.1}%"))
            .unwrap_or("—".into());
        let source = result
            .likely_source
            .as_deref()
            .unwrap_or("—")
            .replace('|', "\\|");
        out.push_str(&format!(
            "| `{}` | {} | {} | {} | {} |\n",
            result.asset.replace('|', "\\|"),
            result.status,
            p95,
            delta,
            source
        ));
    }
    if report.changed_only {
        out.push_str("\n> `--changed` fingerprints `.blend` bytes and dependency paths recorded when the baseline was created.\n");
    }
    out
}

fn write_markdown_outputs(
    report: &ProjectReport,
    markdown: Option<&Path>,
    github_summary: bool,
) -> Result<()> {
    if markdown.is_none() && !github_summary {
        return Ok(());
    }
    let content = markdown_project_report(report);
    if let Some(path) = markdown {
        if let Some(parent) = path.parent() {
            if !parent.as_os_str().is_empty() {
                fs::create_dir_all(parent)?;
            }
        }
        fs::write(path, &content).with_context(|| format!("writing {}", path.display()))?;
    }
    if github_summary {
        let summary_path = env::var_os("GITHUB_STEP_SUMMARY")
            .map(PathBuf::from)
            .context(
            "--github-summary requires GITHUB_STEP_SUMMARY (normally provided by GitHub Actions)",
        )?;
        let mut file = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&summary_path)
            .with_context(|| format!("opening {}", summary_path.display()))?;
        file.write_all(content.as_bytes())?;
        file.write_all(b"\n")?;
    }
    Ok(())
}

fn run_project_baseline(
    root: PathBuf,
    blender_path: Option<PathBuf>,
    overrides: BenchOverrides,
    workers_override: Option<usize>,
    cli_timeout: u64,
    json: bool,
) -> Result<bool> {
    let root = absolute_existing(root)?;
    if !root.is_dir() {
        bail!("project target is not a directory: {}", root.display());
    }
    let trace_dir = root.join(".scenetrace");
    fs::create_dir_all(&trace_dir)?;
    let config = load_project_config(&trace_dir)?;
    let jobs = project_asset_jobs(&root, &trace_dir, &config)?;
    if jobs.is_empty() {
        bail!(
            "no .blend assets matched the project configuration in {}",
            root.display()
        );
    }
    let discovered = jobs.len();
    let workers = default_worker_count(&config, workers_override, discovered);
    let timeout = effective_timeout(&config, cli_timeout);
    let blender = discover_blender(blender_path)?;
    let script = headless_script_path()?;
    let run = ProjectRunContext {
        blender: &blender,
        script: &script,
        config: &config,
        overrides: &overrides,
        timeout_seconds: timeout,
    };

    let results = run_project_baseline_jobs(jobs, workers, |job| project_baseline_asset(job, &run));

    let mut cache = load_cache(&trace_dir)?;
    for result in &results {
        if result.status == "BASELINED" {
            let entry = cache.assets.entry(result.asset.clone()).or_default();
            entry.baseline_hash = Some(result.file_hash.clone());
            entry.last_result = Some("BASELINED".into());
            entry.p95_ms = result.p95_ms;
        }
    }
    save_cache(&trace_dir, &cache)?;
    let report = make_project_report(&root, "baseline", workers, false, discovered, results);
    if json {
        println!("{}", serde_json::to_string_pretty(&report)?);
    } else {
        print_project_report(&report, &blender);
    }
    if report.errors > 0 {
        bail!("project baseline completed with {} errors", report.errors);
    }
    Ok(false)
}

#[allow(clippy::too_many_arguments)]
fn run_project_test(
    root: PathBuf,
    blender_path: Option<PathBuf>,
    overrides: BenchOverrides,
    threshold_percent: Option<f64>,
    min_delta_ms: Option<f64>,
    changed_only: bool,
    workers_override: Option<usize>,
    cli_timeout: u64,
    json: bool,
    markdown: Option<PathBuf>,
    github_summary: bool,
) -> Result<bool> {
    let root = absolute_existing(root)?;
    if !root.is_dir() {
        bail!("project target is not a directory: {}", root.display());
    }
    let trace_dir = root.join(".scenetrace");
    fs::create_dir_all(&trace_dir)?;
    let config = load_project_config(&trace_dir)?;
    let all_jobs = project_asset_jobs(&root, &trace_dir, &config)?;
    if all_jobs.is_empty() {
        bail!(
            "no .blend assets matched the project configuration in {}",
            root.display()
        );
    }
    let discovered = all_jobs.len();
    let mut cache = load_cache(&trace_dir)?;
    let mut skipped = Vec::new();
    let mut jobs = Vec::new();
    for job in all_jobs {
        let baseline_exists = job.trace_dir.join("headless-baseline.json").is_file();
        let unchanged = cache
            .assets
            .get(&job.relative)
            .and_then(|entry| entry.last_success_hash.as_deref())
            == Some(job.file_hash.as_str());
        if changed_only && baseline_exists && unchanged {
            skipped.push(AssetResult::skipped(&job));
        } else {
            jobs.push(job);
        }
    }

    let worker_basis = jobs.len().max(1);
    let workers = default_worker_count(&config, workers_override, worker_basis);
    let timeout = effective_timeout(&config, cli_timeout);
    let threshold = threshold_percent
        .or(config.budget.regression_percent)
        .unwrap_or(20.0);
    let min_delta = min_delta_ms.or(config.budget.min_delta_ms).unwrap_or(2.0);
    let blender = discover_blender(blender_path)?;
    let script = headless_script_path()?;
    let run = ProjectRunContext {
        blender: &blender,
        script: &script,
        config: &config,
        overrides: &overrides,
        timeout_seconds: timeout,
    };

    let mut results = if jobs.is_empty() {
        Vec::new()
    } else {
        run_parallel(jobs, workers, |job| {
            project_test_asset(job, &run, threshold, min_delta)
        })
    };
    results.extend(skipped);
    results.sort_by(|a, b| a.asset.cmp(&b.asset));

    for result in &results {
        let entry = cache.assets.entry(result.asset.clone()).or_default();
        entry.last_result = Some(result.status.clone());
        entry.p95_ms = result.p95_ms.or(entry.p95_ms);
        if result.status == "PASS" {
            entry.last_success_hash = Some(result.file_hash.clone());
        }
    }
    save_cache(&trace_dir, &cache)?;

    let report = make_project_report(&root, "test", workers, changed_only, discovered, results);
    write_markdown_outputs(&report, markdown.as_deref(), github_summary)?;
    if json {
        println!("{}", serde_json::to_string_pretty(&report)?);
    } else {
        print_project_report(&report, &blender);
    }

    if report.errors > 0 {
        bail!(
            "project test completed with {} benchmark errors",
            report.errors
        );
    }
    Ok(report.failed > 0)
}

fn run_single_baseline(
    blend: PathBuf,
    blender_path: Option<PathBuf>,
    overrides: BenchOverrides,
    timeout_seconds: u64,
) -> Result<bool> {
    let blend = resolve_blend_path(blend)?;
    let trace_dir = trace_dir_for_blend(&blend)?;
    let config = load_project_config(&trace_dir)?;
    let settings = resolve_with_overrides(&config, None, &overrides);
    let blender = discover_blender(blender_path)?;
    let script = headless_script_path()?;
    let operation = FileBaselineOperation {
        blend: &blend,
        trace_dir: &trace_dir,
        blender: &blender,
        script: &script,
        settings: &settings,
        timeout_seconds,
    };
    let baseline_path = operation.output_path();

    println!("SceneTrace 1.0 headless baseline");
    println!("Scene:    {}", blend.display());
    println!("Blender:  {}", blender.display());
    println!("Output:   {}", baseline_path.display());
    println!("Runs:     {} calibration runs", settings.calibration_runs);
    println!();
    let baseline = operation.run()?;
    println!(
        "Baseline created: P95 {:.2} ms · noise +/-{:.1}% · {}",
        baseline.run.summary.p95_ms, baseline.noise.p95.percent, baseline.quality
    );
    println!("Result: BASELINE SAVED");
    Ok(false)
}

#[allow(clippy::too_many_arguments)]
fn run_single_test(
    blend: PathBuf,
    blender_path: Option<PathBuf>,
    overrides: BenchOverrides,
    threshold_percent: Option<f64>,
    min_delta_ms: Option<f64>,
    timeout_seconds: u64,
    json: bool,
) -> Result<bool> {
    let blend = resolve_blend_path(blend)?;
    let trace_dir = trace_dir_for_blend(&blend)?;
    fs::create_dir_all(&trace_dir)?;
    let baseline_path = trace_dir.join("headless-baseline.json");
    if !baseline_path.is_file() {
        bail!(
            "no headless baseline found at {}. Create one first with: scenetrace baseline \"{}\"",
            baseline_path.display(),
            blend.display()
        );
    }
    let config = load_project_config(&trace_dir)?;
    let defaults = load_benchmark_defaults(&baseline_path)?;
    let settings = resolve_with_overrides(&config, Some(&defaults), &overrides);
    let threshold = threshold_percent
        .or(config.budget.regression_percent)
        .unwrap_or(20.0);
    let min_delta = min_delta_ms.or(config.budget.min_delta_ms).unwrap_or(2.0);
    let blender = discover_blender(blender_path)?;
    let script = headless_script_path()?;
    let latest_path = trace_dir.join("headless-latest.json");
    let log_path = trace_dir.join("headless.log");
    let mut args = base_runner_args("test", &latest_path, &settings);
    args.extend([
        "--baseline".into(),
        baseline_path.display().to_string(),
        "--threshold-percent".into(),
        threshold.to_string(),
        "--min-delta-ms".into(),
        min_delta.to_string(),
    ]);

    if !json {
        println!("SceneTrace 1.0 headless test");
        println!("Scene:    {}", blend.display());
        println!("Blender:  {}", blender.display());
        println!();
    }
    run_blender(&blender, &blend, &script, &args, &log_path, timeout_seconds)?;
    if !latest_path.is_file() {
        bail!(
            "Blender exited successfully but did not create {}",
            latest_path.display()
        );
    }
    if let Some(reason) = environment_incompatibility(&latest_path)? {
        bail!("environment-incompatible measurement: {reason}");
    }
    execute_compare(
        &baseline_path,
        &latest_path,
        Some(&trace_dir),
        threshold,
        min_delta,
        json,
        "SceneTrace 1.0 headless regression check",
    )
}

fn main() -> ExitCode {
    match run() {
        Ok(failed) => {
            if failed {
                ExitCode::from(1)
            } else {
                ExitCode::SUCCESS
            }
        }
        Err(err) => {
            eprintln!("error: {err:#}");
            ExitCode::from(2)
        }
    }
}

fn run() -> Result<bool> {
    let cli = Cli::parse();
    match cli.command {
        Commands::Compare {
            input,
            current,
            threshold_percent,
            min_delta_ms,
            json,
        } => {
            let (baseline_path, current_path, source_dir) = resolve_paths(input, current)?;
            execute_compare(
                &baseline_path,
                &current_path,
                source_dir.as_deref(),
                threshold_percent,
                min_delta_ms,
                json,
                "SceneTrace 1.0 regression check",
            )
        }

        Commands::Baseline {
            target,
            blender_path,
            frame_start,
            frame_end,
            frame_step,
            repetitions,
            warmups,
            calibration_runs,
            no_modifier_timings,
            workers,
            timeout_seconds,
            json,
        } => {
            let target = absolute_existing(target)?;
            let overrides = BenchOverrides {
                frame_start,
                frame_end,
                frame_step,
                repetitions,
                warmups,
                calibration_runs,
                no_modifier_timings,
            };
            if target.is_dir() {
                run_project_baseline(
                    target,
                    blender_path,
                    overrides,
                    workers,
                    timeout_seconds,
                    json,
                )
            } else {
                if json {
                    bail!(
                        "--json for baseline is currently supported in project-directory mode only"
                    );
                }
                run_single_baseline(target, blender_path, overrides, timeout_seconds)
            }
        }

        Commands::Test {
            target,
            blender_path,
            frame_start,
            frame_end,
            frame_step,
            repetitions,
            warmups,
            no_modifier_timings,
            threshold_percent,
            min_delta_ms,
            changed,
            workers,
            timeout_seconds,
            json,
            markdown,
            github_summary,
        } => {
            let target = absolute_existing(target)?;
            let overrides = BenchOverrides {
                frame_start,
                frame_end,
                frame_step,
                repetitions,
                warmups,
                calibration_runs: None,
                no_modifier_timings,
            };
            if target.is_dir() {
                run_project_test(
                    target,
                    blender_path,
                    overrides,
                    threshold_percent,
                    min_delta_ms,
                    changed,
                    workers,
                    timeout_seconds,
                    json,
                    markdown,
                    github_summary,
                )
            } else {
                if changed {
                    bail!("--changed is a project-directory option; pass a directory such as `scenetrace test . --changed`");
                }
                if markdown.is_some() || github_summary {
                    bail!("--markdown and --github-summary are project-directory options in SceneTrace 1.0");
                }
                run_single_test(
                    target,
                    blender_path,
                    overrides,
                    threshold_percent,
                    min_delta_ms,
                    timeout_seconds,
                    json,
                )
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::sync::atomic::{AtomicUsize, Ordering};

    fn unique_temp_dir(label: &str) -> PathBuf {
        let nanos = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        env::temp_dir().join(format!("scenetrace-{label}-{}-{nanos}", std::process::id()))
    }

    #[test]
    fn extracts_calibrated_baseline_envelope() {
        let value = json!({
            "schema": "scenetrace-baseline",
            "calibration_runs": 5,
            "noise": {"p95": {"percent": 6.9, "ms": 0.12, "samples": 5}, "quality": "GOOD"},
            "frame_noise": {"1": {"percent": 4.0, "ms": 0.05, "samples": 5}},
            "aggregate": {
                "version": 7,
                "measurement_mode": "depsgraph_frame_update_wall_time",
                "samples": [{"frame": 1, "ms": 1.5}],
                "summary": {"median_ms": 1.5, "p95_ms": 1.5, "worst_ms": 1.5, "worst_frame": 1}
            }
        });
        let run = run_from_value(&value, Some("aggregate"), "test").unwrap();
        assert_eq!(run.summary.p95_ms, 1.5);
        let noise: NoiseProfile = serde_json::from_value(value["noise"].clone()).unwrap();
        assert_eq!(noise.p95.percent, 6.9);
    }

    #[test]
    fn resolves_config_and_cli_precedence() {
        let config = ProjectConfig {
            project: ProjectSection::default(),
            frames: FrameConfig {
                start: Some(10),
                end: Some(50),
                step: Some(2),
            },
            benchmark: BenchmarkConfig {
                warmups: Some(2),
                repetitions: Some(4),
                calibration_runs: Some(5),
                capture_modifier_timings: Some(false),
            },
            budget: BudgetConfig::default(),
            workers: WorkerConfig::default(),
        };
        let settings =
            resolve_bench_settings(&config, None, Some(1), None, None, None, None, None, false);
        assert_eq!(settings.frame_start, Some(1));
        assert_eq!(settings.frame_end, Some(50));
        assert_eq!(settings.frame_step, 2);
        assert_eq!(settings.repetitions, 4);
        assert_eq!(settings.warmups, 2);
        assert_eq!(settings.calibration_runs, 5);
        assert!(!settings.capture_modifier_timings);
    }

    #[test]
    fn project_discovery_respects_include_and_exclude() {
        let root = unique_temp_dir("discover");
        fs::create_dir_all(root.join("characters")).unwrap();
        fs::create_dir_all(root.join("archive")).unwrap();
        fs::write(root.join("characters").join("hero.blend"), b"hero").unwrap();
        fs::write(root.join("archive").join("old.blend"), b"old").unwrap();
        fs::write(root.join("notes.txt"), b"notes").unwrap();
        let config = ProjectConfig {
            project: ProjectSection {
                include: vec!["**/*.blend".into()],
                exclude: vec!["archive/**".into()],
            },
            ..ProjectConfig::default()
        };
        let assets = discover_assets(&root, &config).unwrap();
        assert_eq!(assets.len(), 1);
        assert!(assets[0].ends_with("hero.blend"));
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn project_baseline_dispatches_one_scene_once_without_directory_recursion() {
        let root = unique_temp_dir("baseline-dispatch");
        fs::create_dir_all(&root).unwrap();
        fs::write(root.join("scene.blend"), b"scene").unwrap();
        let root = absolute_existing(root).unwrap();

        let jobs = project_asset_jobs(&root, &root.join(".scenetrace"), &ProjectConfig::default())
            .unwrap();
        let dispatches = AtomicUsize::new(0);
        let results = run_project_baseline_jobs(jobs, 1, |job| {
            dispatches.fetch_add(1, Ordering::Relaxed);
            assert!(job.blend.is_file());
            AssetResult {
                asset: job.relative,
                status: "BASELINED".into(),
                p95_ms: None,
                p95_delta_percent: None,
                pattern: None,
                confidence: None,
                likely_source: None,
                error: None,
                file_hash: job.file_hash,
            }
        });

        assert_eq!(dispatches.load(Ordering::Relaxed), 1);
        assert_eq!(results.len(), 1);
        assert_eq!(results[0].asset, "scene.blend");
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn project_discovery_finds_nested_scenes_and_skips_metadata() {
        let root = unique_temp_dir("nested-discover");
        for relative in [
            "characters/hero.blend",
            "environments/forest.blend",
            "environments/city/day.blend",
            ".scenetrace/assets/stale.blend",
        ] {
            let path = root.join(relative);
            fs::create_dir_all(path.parent().unwrap()).unwrap();
            fs::write(path, b"scene").unwrap();
        }
        let root = absolute_existing(root).unwrap();

        let assets = discover_assets(&root, &ProjectConfig::default()).unwrap();
        let relative: Vec<String> = assets
            .iter()
            .map(|path| normalize_relative(path.strip_prefix(&root).unwrap()))
            .collect();

        assert_eq!(
            relative,
            [
                "characters/hero.blend",
                "environments/city/day.blend",
                "environments/forest.blend",
            ]
        );
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn asset_storage_key_is_stable_and_path_specific() {
        let a = asset_storage_key("characters/hero.blend");
        let b = asset_storage_key("backup/hero.blend");
        assert_eq!(a, asset_storage_key("characters/hero.blend"));
        assert_ne!(a, b);
        assert!(a.starts_with("hero-"));
    }

    #[test]
    fn file_hash_changes_when_asset_changes() {
        let root = unique_temp_dir("hash");
        fs::create_dir_all(&root).unwrap();
        let path = root.join("hero.blend");
        fs::write(&path, b"before").unwrap();
        let before = hash_file(&path).unwrap();
        fs::write(&path, b"after").unwrap();
        let after = hash_file(&path).unwrap();
        assert_ne!(before, after);
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn blender_command_uses_factory_startup_before_background_mode() {
        let command = blender_command(
            Path::new("blender"),
            Path::new("scene.blend"),
            Path::new("runner.py"),
            &["test".into()],
        );
        let args: Vec<_> = command
            .get_args()
            .map(|arg| arg.to_string_lossy().into_owned())
            .collect();

        assert_eq!(
            args,
            [
                "--factory-startup",
                "--background",
                "scene.blend",
                "--python",
                "runner.py",
                "--",
                "test",
            ]
        );
    }

    #[test]
    fn project_cache_is_versioned_and_does_not_leave_a_predictable_temp_file() {
        let root = unique_temp_dir("cache-persistence");
        fs::create_dir_all(&root).unwrap();

        save_cache(&root, &ProjectCache::default()).unwrap();

        let cache_path = root.join("cache.json");
        let value = read_json(&cache_path).unwrap();
        assert_eq!(value["schema"], "scenetrace-project-cache");
        assert_eq!(value["schema_version"], 1);
        assert!(!root.join("cache.tmp").exists());

        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn asset_fingerprint_changes_when_a_recorded_dependency_changes() {
        let root = unique_temp_dir("dependency-fingerprint");
        fs::create_dir_all(&root).unwrap();
        let scene = root.join("scene.blend");
        let texture = root.join("texture.png");
        let trace_dir = root.join(".scenetrace");
        fs::write(&scene, b"scene").unwrap();
        fs::write(&texture, b"first").unwrap();
        fs::create_dir_all(&trace_dir).unwrap();
        fs::write(
            trace_dir.join("headless-baseline.json"),
            serde_json::json!({"dependencies": [{"path": texture}]}).to_string(),
        )
        .unwrap();

        let before = asset_fingerprint(&scene, &trace_dir).unwrap();
        fs::write(&texture, b"second").unwrap();
        let after = asset_fingerprint(&scene, &trace_dir).unwrap();

        assert_ne!(before, after);
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn rejects_newer_baseline_schema_without_reinterpreting_it() {
        let root = unique_temp_dir("newer-baseline-schema");
        fs::create_dir_all(&root).unwrap();
        let baseline = root.join("headless-baseline.json");
        fs::write(
            &baseline,
            json!({
                "schema": "scenetrace-baseline",
                "schema_version": 2,
                "aggregate": {}
            })
            .to_string(),
        )
        .unwrap();

        let error = load_baseline(&baseline).unwrap_err();
        assert!(error.to_string().contains("newer schema version 2"));
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn identifies_environment_incompatibility_as_an_execution_error() {
        let root = unique_temp_dir("environment-incompatible");
        fs::create_dir_all(&root).unwrap();
        let latest = root.join("headless-latest.json");
        fs::write(
            &latest,
            json!({
                "comparison": {
                    "classification": "environment_incompatible",
                    "environment": {"reasons": ["Blender major/minor version differs"]}
                }
            })
            .to_string(),
        )
        .unwrap();

        assert_eq!(
            environment_incompatibility(&latest).unwrap().as_deref(),
            Some("Blender major/minor version differs")
        );
        fs::remove_dir_all(root).unwrap();
    }
}
