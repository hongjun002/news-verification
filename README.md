# News Verification Course Project

This project reproduces a multi-step news verification workflow and includes a single-call model baseline for comparison.

## 1. Configure the OpenRouter API key

Set the API key as an environment variable before running the project. Do not hard-code the key before uploading the project to GitHub.

```bash
export OPENROUTER_API_KEY="your OpenRouter API key here"
```

The model is configured in `config.py`. The current model is fixed as:

```text
openai/gpt-4.1-mini
```

## 2. Reproduce all experiments

In GitHub Codespaces or a local terminal, enter the project folder and run:

```bash
python3 run_all_experiments.py
```

The script runs the experiments in this order:

1. It first runs the multi-step project workflow in `main.py` on the three test sets in `test_sets`, processing each news item one by one and generating the project-flow test report.
2. It then runs the single-call baseline in `single_call_experiment.py` on the same three test sets, again processing each news item one by one and generating the baseline test report.

Each Markdown file is processed item by item. One news item is completed and saved before the next item starts.

The main output reports are written to:

```text
results/project_flow_test_report.md
results/single_call_test_report.md
```

## 3. Run only the multi-step project workflow

By default, the script reads `test_news.md`:

```bash
python3 main.py
```

You can also specify a test set and output folder:

```bash
python3 main.py --input test_sets/news_text_evaluation_A.md --output-dir results/project_flow_A
```

## 4. Run only the single-call baseline

By default, the script reads `test_news.md`:

```bash
python3 single_call_experiment.py
```

You can also specify a test set and output folder:

```bash
python3 single_call_experiment.py --input test_sets/news_text_evaluation_A.md --output-dir results/single_call_A
```

## 5. File guide

```text
config.py                    API key, model, input path, and output path configuration
main.py                      Main workflow: claim extraction, fact checking, logic checking, and programmatic scoring
single_call_experiment.py    Baseline workflow: one model call per news item
run_all_experiments.py       One-command reproduction for three test sets and two workflows
test_sets/                   Course-project test sets
results/                     Generated results after running the project
```

This project uses only the Python standard library. No third-party dependencies are required.
