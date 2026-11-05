"""CLI research, focused recovery, and tool-free finalization.

The model's turn cap is a scheduling boundary, never a paper verdict. Durable
batch checkpoints prevent an outer retry from resetting progress. All phases
share the caller's single whole-paper deadline; there are no independent
phase timers that can kill a valid investigation early.
"""
import asyncio
import hashlib
import json
import logging
import os
import re
import shlex
import time
import uuid
from pathlib import Path

log = logging.getLogger(__name__)


def investigation_budget(phase):
    """Return only the remaining whole-paper deadline.

    There is deliberately no per-phase timeout.  A research, recovery, or
    finalization phase may need the full time left for this paper; slicing it
    into fixed 120/240/480-second windows caused valid CLI sessions to be
    killed before they could emit a result.  The caller's outer paper
    deadline remains the single time boundary and cancels/reaps the process
    when it is actually reached.
    """
    from api.services.review_runtime import paper_deadline
    deadline = paper_deadline.get()
    if deadline is None:
        return None
    return max(0.1, deadline - time.monotonic())


def schema_only_gaps(gaps):
    """Formatting recovery may not suppress missing scientific evidence."""
    prefixes = ('Verdict must agree', 'Evidence needs a valid issue_index',
                'Duplicate issue numbers', 'Result must contain an explicit',
                'image维度缺少明确风险结论', 'data维度缺少明确风险结论',
                'ref维度缺少明确风险结论', 'methodology维度缺少明确风险结论')
    return bool(gaps) and all(str(g).startswith(prefixes) for g in gaps)


def prompt_key(prompt):
    return hashlib.sha256(re.sub(r'review_draft_[0-9a-f]{32}\.json',
                                'review_draft_CHECKPOINT.json', prompt).encode()).hexdigest()[:20]


def preflight_paths(prompt):
    """Use only the per-invocation paths emitted by our prompt builder."""
    for line in prompt.splitlines():
        if 'scripts/validate_review_result.py ' not in line:
            continue
        try:
            args = shlex.split(line)
            paths = {name: args[args.index('--' + name) + 1]
                     for name in ('result', 'evidence', 'doi')}
            if re.fullmatch(r'review_draft_[0-9a-f]{32}\.json', Path(paths['result']).name):
                return paths
        except (ValueError, IndexError):
            pass
    return {}


def checked_draft(doi, paths):
    """Recover a completed file, never a guessed verdict or incomplete draft."""
    if not paths or paths.get('doi', '').casefold() != doi.casefold():
        return None, []
    try:
        result = json.loads(Path(paths['result']).read_text(encoding='utf-8'))
        evidence = json.loads(Path(paths['evidence']).read_text(encoding='utf-8'))
        if not isinstance(result, dict) or str(result.get('doi', '')).casefold() != doi.casefold():
            return None, ['Saved draft DOI mismatch']
        from api.services.review_validation import validate
        checked, gaps = validate(result, evidence)
        return (checked if not gaps else None), gaps
    except (OSError, ValueError, TypeError, KeyError):
        return None, []


def transport_failure(error):
    """A turn limit or schema defect is not a failed model endpoint."""
    return bool(re.search(r'timeout|timed out|rate.?limit|overload|connection|'
                          r'\b(?:429|500|502|503|504)\b|authentication|unauthorized',
                          str(error), re.I))


async def run(engine, doi, label, prompt, max_turns, allowed_tools=('Bash', 'Read'),
              *, state=None, persist=None, response_sink=None, progress=None):
    state = dict(state or {})
    if state.get('completed_result'):
        return state['completed_result']
    # Old checkpoints may already have spent many 160-turn continuations.
    # Give those sessions only the focused recovery and finalization budgets.
    start = state.get('phase', 1 if state.get('session_id') else 0)
    # Migrate only the old one-turn finalizer that actually hit its turn cap.
    # A hallucinated Write call consumed that single turn without producing
    # stdout. Allow one bounded output-only correction, never new research.
    metrics = state.get('phase_metrics') or []
    if (start == 3 and state.get('finalization_version', 0) < 2 and metrics
            and metrics[-1].get('phase') == 2
            and metrics[-1].get('subtype') == 'error_max_turns'):
        start = 2
        state['legacy_output_correction'] = True
    if (start >= 3 and not state.get('completed_result')
            and not state.get('output_gaps')
            and not any(item.get('phase') == 2 for item in metrics)):
        start = 2
    session = state.get('session_id') or str(uuid.uuid4())
    last_error = state.get('last_error', 'review session exhausted')
    output_gaps = state.get('output_gaps') or []
    # A resumed checkpoint can already be past finalization after its JSON
    # failed publication validation. Give the saved draft one focused repair
    # pass before returning a process error; otherwise a valid substantive
    # review is lost solely because the final response omitted a HIGH item.
    if (start >= 3 and output_gaps and not state.get('post_validation_recovery_attempted')):
        start = 1
        state['post_validation_recovery_attempted'] = True
    # A finalizer can correctly preserve a review_error when the evidence is
    # still incomplete.  Its checkpoint is nevertheless at phase 3, so a
    # later task retry must reopen the focused recovery phase instead of
    # returning the same fallback without starting a CLI process.  This is
    # granted once, so repeated retries cannot keep refunding the recovery
    # budget; a migrated legacy finalizer already used its one correction.
    if (start >= 3 and not state.get('completed_result')
            and not output_gaps and metrics
            and any(item.get('phase') == 2 for item in metrics)
            and not state.get('continuation_attempts')
            and not state.get('legacy_output_correction')
            and last_error not in {'', 'review session exhausted'}):
        start = 1
        state['continuation_attempts'] = int(state.get('continuation_attempts', 0)) + 1
    # A resumed builder allocates a different random draft filename. Keep the
    # original path with the session so a saved result remains recoverable.
    state.setdefault('preflight', preflight_paths(prompt))
    # Recovery should only close evidence/format gaps found in the saved
    # checkpoint. With the previous 24-turn allowance it could become a second
    # full investigation after the initial pass had already consumed most of
    # the paper deadline.
    recovery_turn_cap = max(6, int(os.environ.get('REVIEW_CLI_RECOVERY_MAX_TURNS', str(max_turns))))
    focused_turns = min(recovery_turn_cap, max_turns)
    final_turns = max(3, int(os.environ.get('REVIEW_CLI_FINAL_MAX_TURNS', '12')))
    focused = (
        '接续本会话核查。先整理已验证事实和未解决项，仅对尚未解决项做必要核查；'
        '当前只提供Bash和Read工具，Write和Edit不可用；如需保存草稿，使用Bash执行。'
        '禁止重复全表扫描、逐条重新核查已完成的参考文献或重做图像比对。'
        f'本阶段最多{focused_turns}轮，必须预留最后一轮按原任务格式输出完整JSON。'
        '保留各维度结论和每类必查项的具体依据，明确材料限制；'
        '流程错误不得判为论文风险，未核查项目不得声称已核查。'
        '数值复算必须使用解析公式、向量化计算或明确很小的枚举；禁止组合爆搜、'
        '无界 combinations_with_replacement、遍历百万以上候选或单个脚本运行超过120秒。'
        '任何脚本先设置 timeout 120，并优先给出可复核的近似/边界结论。'
    )
    finalize = (
        '现在结束工具调查，只用本会话已经核验的证据按原任务字段直接回复最终JSON。'
        '本阶段所有工具均不可用，包括Write、Edit、Bash和Read。'
        '不要保存或修改任何文件，不要输出工具调用；把完整JSON对象作为本条回复正文。'
        '保留有依据的四维结论、具体位置和必查项解释，不得新增未经核查的事实。'
        '若关键证据仍不足以形成完整结论，输出 '
        '{"doi":"' + doi + '","trigger":"review_error","_review_error":"具体未解决项"}；'
        '不能仅因达到轮数上限判为高风险或低风险。不要再调用任何工具。'
        '完整结论必须包含 doi、result、verdict、trigger、image_review、data_review、'
        'ref_review、methodology_review、reason、dimension_verdicts；意见字段必须是字符串。'
        'dimension_verdicts 必须分别给出 image、data、ref、methodology 的有依据结论。'
        'result严格二选一：高风险/低风险；verdict和四个dimension_verdicts的值严格二选一：'
        '建议高风险/建议低风险，不得在这些标签值中附带括号、解释或标点。'
        '解释写入对应的*_review字段；有风险问题沿用原任务要求的编号和'
        '问题位置、问题类型、发现、为什么重要、核查依据结构，截图issue_index对应问题编号。'
        '保持每类必查线索的具体结论与依据；只整理既有事实，不补造证据。'
        '若证据包含有HIGH的正文内容冲突，必须在data_review或reason中明确写出“内容冲突”或“正文不一致”，'
        '并紧接着给出该项属于异常/问题、可解释或正常的明确结论；不能只描述位置而省略结论。'
        '每个维度的确认问题必须用可识别的阿拉伯编号（如（1）、（2）或1.、2.）独立起段，'
        '并让evidence_images和excel_evidence_regions中的issue_index严格对应该维度的问题编号；'
        '不要只使用“一、二、三”作为问题编号。'
    )
    scheduled_prompt = prompt + (
        '\n本阶段只有Bash和Read工具；Write和Edit不可用。保存草稿请通过Bash执行。'
        f'\n本阶段最多{max_turns}轮；请在前{max(1, max_turns - 8)}轮内完成调查并保存草稿，'
        '最后至少6轮只用于校验和输出。已有确定性扫描应查代表原值，'
        '无新增矛盾不重做全量扫描。外部请求请设置超时，同一失败不要反复请求。\n'
        '性能硬约束：禁止组合爆搜和无界枚举；数值问题用解析推导或向量化计算，'
        '每个辅助脚本必须有 timeout 120，单次候选数不得超过100000。\n'
    )
    # Leave room for the CLI to correct a rejected tool call. Tools remain
    # disabled, so these turns cannot restart investigation.
    phases = ((max(1, max_turns), scheduled_prompt), (focused_turns, focused), (final_turns, finalize))

    # A turn cap is not evidence that the investigation is complete.  Only
    # skip the focused phase when a complete draft already exists and the
    # validator found formatting-only gaps.  If the model exhausted turns
    # before producing a draft, or left substantive coverage gaps, give it one
    # short, targeted continuation.  This preserves accuracy without reviving
    # the old unbounded second full investigation.
    skip_focused_recovery = (
        start == 1
        and last_error == 'error_max_turns'
        and bool(state.get('candidate_result'))
        and schema_only_gaps(state.get('output_gaps') or [])
    )

    def complete(result, source):
        result = engine._normalize_review_result(result)
        state.update(completed_result=result, result_source=source, finished_at=time.time())
        if persist:
            persist(dict(state))
        return result

    # Even an exhausted checkpoint can hold a valid file whose stdout was lost.
    if start:
        draft, _ = checked_draft(doi, state['preflight'])
        if draft:
            log.info('%s DOI=%s recovered validated draft without another model call', label, doi)
            return complete(draft, 'validated_draft')
    for phase in range(start, len(phases)):
        if phase == 1 and skip_focused_recovery:
            continue
        turns, instruction = phases[phase]
        # A killed/turn-limited CLI can leave a complete but invalid JSON file
        # without any stdout. Carry its precise gaps into output-only recovery,
        # rather than starting another scientific investigation for formatting.
        saved, saved_gaps = checked_draft(doi, state['preflight'])
        if saved:
            return complete(saved, 'validated_draft')
        if saved_gaps and state['preflight']:
            try:
                candidate = json.loads(Path(state['preflight']['result']).read_text(encoding='utf-8'))
                if (isinstance(candidate, dict) and candidate.get('doi', '').casefold() == doi.casefold()
                        and candidate.get('trigger') != 'review_error'):
                    output_gaps = saved_gaps
                    state.update(candidate_result=candidate, output_gaps=output_gaps)
            except (OSError, ValueError, TypeError):
                pass
        output_only = not allowed_tools or phase == 2 or (phase == 1 and schema_only_gaps(output_gaps))
        if output_gaps:
            instruction += '\n发布校验尚未通过：' + json.dumps(output_gaps, ensure_ascii=False)
        if phase == 1 and last_error not in {'', 'review session exhausted'}:
            instruction += '\n上一次恢复未完成的具体记录（仅作为交接信息，必须继续核查）：' + last_error[:12000]
        if output_only and state.get('candidate_result'):
            instruction += ('\n下方为本会话已有的完整草稿；仅修复字段、编号和截图对应关系，'
                            '不得新增未经核查的事实。必须直接输出完整JSON：\n'
                            + json.dumps(state['candidate_result'], ensure_ascii=False))
        model = state.get('active_model') or engine.REVIEW_MODEL
        if phase and transport_failure(last_error):
            model = getattr(engine, 'REVIEW_FALLBACK_MODEL', '') or model
        if phase:
            _, gaps = checked_draft(doi, state['preflight'])
            if gaps:
                instruction += '\n已保存草稿尚需解决的具体校验项：' + json.dumps(gaps, ensure_ascii=False)
            if state['preflight'] and phase == 1:
                instruction += '\n沿用本会话草稿路径：' + state['preflight']['result']
        resume_session = phase > 0
        if resume_session and last_error == 'CLI resume session not found':
            # CLI storage may lose a session after a worker crash. Rehydrate
            # the original task and saved evidence, but do not refund phases
            # or turn/time budgets and do not start full research again.
            state.setdefault('lost_session_ids', []).append(session)
            session = str(uuid.uuid4())
            resume_session = False
            instruction = prompt + '\n\n会话记录不可用。沿用保存证据，禁止重做全篇调查。\n' + instruction
        # Very large handoff prompts can exceed the OS execve argument limit
        # (typically around 2 MB), producing ``Argument list too long`` before
        # Claude even starts.  Keep ordinary calls unchanged, but stream long
        # prompts through stdin; Claude's print mode accepts stdin when the
        # prompt positional argument is omitted.
        prompt_via_stdin = len(instruction.encode('utf-8')) >= 256 * 1024
        command = [engine._find_claude_cli(), '-p']
        if not prompt_via_stdin:
            command.append(instruction)
        command.extend([
                   '--model', model, '--output-format', 'json', '--max-turns', str(turns),
                   '--resume' if resume_session else '--session-id', session])
        settings_file = getattr(engine, 'REVIEW_CLAUDE_SETTINGS_FILE', os.environ.get(
            'REVIEW_CLAUDE_SETTINGS_FILE', '/home/library/.claude/settings-mm.json'))
        if settings_file:
            command.extend(['--settings', settings_file])
        if output_only or not allowed_tools:
            command.extend(['--tools', ''])
        else:
            # allowedTools only grants permission; it does not remove other
            # built-ins. Explicitly restrict the available tool set as well.
            command.extend(['--tools', ','.join(allowed_tools), '--allowedTools', *allowed_tools])
        # Persist BEFORE launch: process death/restart cannot reset the budget.
        timeout_seconds = investigation_budget(2 if output_only else phase)
        state.update(session_id=session, phase=phase + 1, label=label, doi=doi,
                     phase_name=('research', 'focused_recovery', 'finalizing')[phase],
                     started_at=time.time(), timeout_seconds=timeout_seconds, active_model=model)
        if phase == 2:
            state['finalization_version'] = 2
        if persist:
            persist(dict(state))
        if progress:
            progress(dict(state))
        log.info('%s DOI=%s phase=%s session=%s max_turns=%d model=%s',
                 label, doi, state['phase_name'], session, turns, model)
        started = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            *command, stdin=asyncio.subprocess.PIPE if prompt_via_stdin else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            cwd=str(engine.BASE_DIR), env=engine._claude_subprocess_env(), start_new_session=True)
        raw = {}
        stderr = b''
        try:
            # All reviewers and repair calls share the caller's paper deadline.
            # Cancellation below reaps the process without starting recovery.
            try:
                # ``timeout(None)`` means no stage timer.  The outer
                # run_review_single deadline is the only review timeout.
                timer = asyncio.timeout(timeout_seconds)
                async with timer:
                    if prompt_via_stdin:
                        stdout, stderr = await proc.communicate(input=instruction.encode('utf-8'))
                    else:
                        stdout, stderr = await proc.communicate()
            except TimeoutError:
                await engine._kill_and_reap_process(proc)
                last_error = ('whole-paper deadline reached; checkpoint retained for recovery'
                              if timer.expired() else 'model transport timeout/connection failure')
                stdout = b'{}'
                stderr = b''
            raw = json.loads(stdout.decode(errors='replace'))
            if isinstance(raw, list):
                raw = raw[0] if raw else {}
            if not isinstance(raw, dict):
                raw = {}
            if raw:
                last_error = str(raw.get('subtype') or f'CLI exit {proc.returncode}: invalid review output')
        except asyncio.CancelledError:
            await engine._kill_and_reap_process(proc)
            raise
        except (TimeoutError, OSError):
            await engine._kill_and_reap_process(proc)
            last_error = 'model transport timeout/connection failure'
        except (ValueError, TypeError):
            # Keep useful categories for resume/launch errors, never credential-
            # bearing endpoint text. A missing session cannot be repaired by
            # repeatedly asking the CLI to resume that same absent session.
            diagnostic = stderr.decode(errors='replace')
            last_error = ('CLI resume session not found' if re.search(
                r'no (?:conversation|session)|session.*not found|conversation.*not found', diagnostic, re.I)
                else 'malformed CLI JSON')
        details = raw.get('errors') or stderr.decode(errors='replace')[-1000:]
        if raw.get('is_error') and details and transport_failure(details):
            # Store only the category; endpoint errors can contain credentials.
            last_error = 'model transport timeout/rate-limit/connection failure'
        metrics = {'phase': phase, 'model': model, 'wall_seconds': round(time.monotonic() - started, 2),
                   'api_ms': raw.get('duration_api_ms'), 'turns': raw.get('num_turns'),
                   'subtype': raw.get('subtype'), 'returncode': proc.returncode}
        state.setdefault('phase_metrics', []).append(metrics)
        log.info('%s DOI=%s phase_metrics=%s', label, doi, json.dumps(metrics))
        if response_sink:
            response_sink(phase, {**state, 'response': raw, 'returncode': proc.returncode,
                                  'stderr': stderr.decode(errors='replace')[-2000:], 'error': last_error})
        if proc.returncode == 0 and not raw.get('is_error'):
            text = raw.get('result', raw.get('content', ''))
            if isinstance(text, list):
                text = ' '.join(b.get('text', '') for b in text if isinstance(b, dict))
            inner = engine._extract_json_from_text(text) if isinstance(text, str) else None
            if isinstance(inner, dict):
                if str(inner.get('doi', doi)).lower() != doi.lower():
                    last_error = 'CLI result DOI mismatch'
                elif inner.get('trigger') == 'review_error':
                    last_error = inner.get('_review_error') or inner.get('reason') or 'unresolved evidence'
                else:
                    # Accept exact transport aliases without inferring a risk
                    # conclusion from prose or filling missing dimensions.
                    risk = inner.get('result')
                    if isinstance(risk, str):
                        inner['result'] = {'low_risk': '低风险', 'high_risk': '高风险'}.get(risk.strip().lower(), risk)
                if (inner.get('trigger') != 'review_error'
                        and str(inner.get('doi', doi)).lower() == doi.lower()
                        and inner.get('result') in ('高风险', '低风险')):
                    inner.setdefault('doi', doi)
                    paths = state['preflight']
                    if paths:
                        from api.services.review_validation import validate
                        try:
                            evidence = json.loads(Path(paths['evidence']).read_text(encoding='utf-8'))
                            checked, output_gaps = validate(inner, evidence)
                        except (OSError, ValueError, TypeError) as exc:
                            output_gaps = [f'Publication evidence unavailable: {exc}']
                        state.update(candidate_result=inner, output_gaps=output_gaps)
                        if not output_gaps:
                            return complete(checked, 'validated_stdout')
                        last_error = 'CLI review failed publication validation: ' + '; '.join(output_gaps)
                    else:
                        return complete(inner, 'stdout')
                if last_error == 'success':
                    last_error = 'CLI completed but returned no valid review risk label'
            elif last_error == 'success':
                last_error = 'CLI completed but returned no review JSON object'
        state['last_error'] = last_error
        if persist:
            persist(dict(state))
        log.warning('%s DOI=%s phase=%s incomplete: %s', label, doi, phase, last_error)
        # Once the sole whole-paper deadline is exhausted, do not start
        # another phase with a near-zero timeout.  The outer caller will
        # convert this into a retryable review error with the checkpoint kept.
        remaining = None
        from api.services.review_runtime import paper_deadline
        if paper_deadline.get() is not None:
            remaining = paper_deadline.get() - time.monotonic()
        if remaining is not None and remaining <= 0:
            break
        # Keep the focused continuation after a turn-limit failure unless a
        # validated draft was already present and only schema formatting gaps
        # remained (the conditional initialized above). A bare turn-limit
        # response must never suppress the only chance to close missing
        # evidence coverage.
        draft, _ = checked_draft(doi, state['preflight'])
        if draft:
            log.info('%s DOI=%s recovered validated draft after phase=%d', label, doi, phase)
            return complete(draft, 'validated_draft')
    result = engine._fallback_result(doi, f'{label}: bounded recovery exhausted: {last_error}')
    result['_retry_exhausted'] = True
    return result
