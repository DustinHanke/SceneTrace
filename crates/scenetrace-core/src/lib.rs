use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct FrameSample {
    pub frame: i32,
    pub ms: f64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct Summary {
    pub median_ms: f64,
    pub p95_ms: f64,
    pub worst_ms: f64,
    pub worst_frame: i32,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct Run {
    pub version: u32,
    pub measurement_mode: String,
    pub samples: Vec<FrameSample>,
    pub summary: Summary,
    #[serde(default)]
    pub scene_snapshot: serde_json::Value,
    #[serde(default)]
    pub settings: serde_json::Value,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize, PartialEq)]
pub struct NoiseEstimate {
    #[serde(default)]
    pub percent: f64,
    #[serde(default)]
    pub ms: f64,
    #[serde(default)]
    pub samples: usize,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize, PartialEq)]
pub struct NoiseProfile {
    #[serde(default)]
    pub p95: NoiseEstimate,
    #[serde(default)]
    pub quality: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct RegressedFrame {
    pub frame: i32,
    pub baseline_ms: f64,
    pub current_ms: f64,
    pub delta_ms: f64,
    pub delta_percent: f64,
    pub effective_threshold_percent: f64,
    pub effective_threshold_ms: f64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct RegressionPattern {
    pub kind: String,
    pub affected_frames: usize,
    pub total_frames: usize,
    pub affected_percent: f64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct Comparison {
    pub median_delta_percent: f64,
    pub p95_delta_percent: f64,
    pub worst_delta_percent: f64,
    pub p95_delta_ms: f64,
    pub effective_p95_threshold_percent: f64,
    pub effective_p95_threshold_ms: f64,
    pub expected_p95_noise_percent: f64,
    pub expected_p95_noise_ms: f64,
    pub regressed_frames: Vec<RegressedFrame>,
    pub pattern: RegressionPattern,
    pub confidence: String,
    pub failed: bool,
}

pub fn percentile(values: &[f64], q: f64) -> f64 {
    if values.is_empty() {
        return 0.0;
    }
    let mut sorted = values.to_vec();
    sorted.sort_by(|a, b| a.total_cmp(b));
    if sorted.len() == 1 {
        return sorted[0];
    }
    let pos = q.clamp(0.0, 1.0) * (sorted.len() - 1) as f64;
    let lower = pos.floor() as usize;
    let upper = pos.ceil() as usize;
    if lower == upper {
        sorted[lower]
    } else {
        let weight = pos - lower as f64;
        sorted[lower] * (1.0 - weight) + sorted[upper] * weight
    }
}

pub fn summarize(samples: &[FrameSample]) -> Summary {
    if samples.is_empty() {
        return Summary {
            median_ms: 0.0,
            p95_ms: 0.0,
            worst_ms: 0.0,
            worst_frame: 0,
        };
    }
    let values: Vec<f64> = samples.iter().map(|s| s.ms).collect();
    let worst = samples
        .iter()
        .max_by(|a, b| a.ms.total_cmp(&b.ms))
        .expect("non-empty samples");
    Summary {
        median_ms: percentile(&values, 0.5),
        p95_ms: percentile(&values, 0.95),
        worst_ms: worst.ms,
        worst_frame: worst.frame,
    }
}

fn pct(base: f64, current: f64) -> f64 {
    if base.abs() < f64::EPSILON {
        if current.abs() < f64::EPSILON {
            0.0
        } else {
            100.0
        }
    } else {
        (current - base) / base * 100.0
    }
}

fn classify_pattern(affected: usize, total: usize) -> RegressionPattern {
    let affected_percent = if total == 0 {
        0.0
    } else {
        affected as f64 / total as f64 * 100.0
    };
    let kind = if affected == 0 {
        "none"
    } else if affected_percent >= 70.0 {
        "persistent"
    } else if affected_percent >= 25.0 {
        "widespread"
    } else {
        "localized"
    };
    RegressionPattern {
        kind: kind.to_string(),
        affected_frames: affected,
        total_frames: total,
        affected_percent,
    }
}

pub fn compare(
    baseline: &Run,
    current: &Run,
    threshold_percent: f64,
    min_delta_ms: f64,
) -> Comparison {
    compare_with_noise(
        baseline,
        current,
        threshold_percent,
        min_delta_ms,
        &NoiseProfile::default(),
        &BTreeMap::new(),
    )
}

pub fn compare_with_noise(
    baseline: &Run,
    current: &Run,
    threshold_percent: f64,
    min_delta_ms: f64,
    noise: &NoiseProfile,
    frame_noise: &BTreeMap<i32, NoiseEstimate>,
) -> Comparison {
    let baseline_by_frame: BTreeMap<i32, f64> =
        baseline.samples.iter().map(|s| (s.frame, s.ms)).collect();

    let mut regressed_frames = Vec::new();
    let mut common_frames = 0usize;
    for sample in &current.samples {
        if let Some(base) = baseline_by_frame.get(&sample.frame) {
            common_frames += 1;
            let delta = sample.ms - *base;
            let delta_percent = pct(*base, sample.ms);
            let learned = frame_noise.get(&sample.frame).cloned().unwrap_or_default();
            let effective_percent = threshold_percent.max(learned.percent * 2.0);
            let effective_ms = min_delta_ms.max(learned.ms * 2.0);
            if delta >= effective_ms && delta_percent >= effective_percent {
                regressed_frames.push(RegressedFrame {
                    frame: sample.frame,
                    baseline_ms: *base,
                    current_ms: sample.ms,
                    delta_ms: delta,
                    delta_percent,
                    effective_threshold_percent: effective_percent,
                    effective_threshold_ms: effective_ms,
                });
            }
        }
    }
    regressed_frames.sort_by(|a, b| b.delta_ms.total_cmp(&a.delta_ms));

    let median_delta_percent = pct(baseline.summary.median_ms, current.summary.median_ms);
    let p95_delta_percent = pct(baseline.summary.p95_ms, current.summary.p95_ms);
    let worst_delta_percent = pct(baseline.summary.worst_ms, current.summary.worst_ms);
    let p95_delta_ms = current.summary.p95_ms - baseline.summary.p95_ms;

    let effective_p95_threshold_percent = threshold_percent.max(noise.p95.percent * 2.0);
    let effective_p95_threshold_ms = min_delta_ms.max(noise.p95.ms * 2.0);
    let pattern = classify_pattern(regressed_frames.len(), common_frames);
    let failed = (p95_delta_percent >= effective_p95_threshold_percent
        && p95_delta_ms >= effective_p95_threshold_ms)
        || !regressed_frames.is_empty();

    let confidence = if !failed {
        "STABLE".to_string()
    } else {
        let signal_ratio = if effective_p95_threshold_percent > 0.0 {
            p95_delta_percent / effective_p95_threshold_percent
        } else {
            99.0
        };
        if signal_ratio >= 2.0 || pattern.affected_percent >= 50.0 {
            "HIGH".to_string()
        } else {
            "MEDIUM".to_string()
        }
    };

    Comparison {
        median_delta_percent,
        p95_delta_percent,
        worst_delta_percent,
        p95_delta_ms,
        effective_p95_threshold_percent,
        effective_p95_threshold_ms,
        expected_p95_noise_percent: noise.p95.percent,
        expected_p95_noise_ms: noise.p95.ms,
        regressed_frames,
        pattern,
        confidence,
        failed,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn run(values: &[(i32, f64)]) -> Run {
        let samples: Vec<FrameSample> = values
            .iter()
            .map(|(frame, ms)| FrameSample {
                frame: *frame,
                ms: *ms,
            })
            .collect();
        let summary = summarize(&samples);
        Run {
            version: 1,
            measurement_mode: "depsgraph_frame_update".into(),
            samples,
            summary,
            scene_snapshot: serde_json::json!({}),
            settings: serde_json::json!({}),
        }
    }

    #[test]
    fn detects_frame_regression() {
        let base = run(&[(1, 10.0), (2, 10.0), (3, 10.0)]);
        let current = run(&[(1, 10.0), (2, 30.0), (3, 10.0)]);
        let comparison = compare(&base, &current, 20.0, 2.0);
        assert!(comparison.failed);
        assert_eq!(comparison.regressed_frames[0].frame, 2);
        assert_eq!(comparison.pattern.kind, "widespread");
    }

    #[test]
    fn learned_noise_can_prevent_small_false_positive() {
        let base = run(&[(1, 10.0), (2, 10.0), (3, 10.0)]);
        let current = run(&[(1, 11.2), (2, 11.2), (3, 11.2)]);
        let noise = NoiseProfile {
            p95: NoiseEstimate {
                percent: 8.0,
                ms: 0.8,
                samples: 5,
            },
            quality: "GOOD".into(),
        };
        let frame_noise = BTreeMap::from([
            (
                1,
                NoiseEstimate {
                    percent: 8.0,
                    ms: 0.8,
                    samples: 5,
                },
            ),
            (
                2,
                NoiseEstimate {
                    percent: 8.0,
                    ms: 0.8,
                    samples: 5,
                },
            ),
            (
                3,
                NoiseEstimate {
                    percent: 8.0,
                    ms: 0.8,
                    samples: 5,
                },
            ),
        ]);
        let comparison = compare_with_noise(&base, &current, 5.0, 0.1, &noise, &frame_noise);
        assert_eq!(comparison.effective_p95_threshold_percent, 16.0);
        assert!(!comparison.failed);
        assert!(comparison.regressed_frames.is_empty());
    }

    #[test]
    fn persistent_regression_is_classified() {
        let base = run(&[(1, 1.5), (2, 1.5), (3, 1.5)]);
        let current = run(&[(1, 75.0), (2, 75.0), (3, 75.0)]);
        let comparison = compare(&base, &current, 20.0, 2.0);
        assert!(comparison.failed);
        assert_eq!(comparison.pattern.kind, "persistent");
        assert_eq!(comparison.confidence, "HIGH");
    }
}
