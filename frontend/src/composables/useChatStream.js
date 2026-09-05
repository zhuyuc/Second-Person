import { ref, computed } from 'vue'
import { formatTimelineSummary } from '@/utils/timelineSummary'

/**
 * 聊天流式状态机：timeline、正文节流、工具步旁白缓冲、finishStream 入列。
 * 宿主（ChatView）注入会话/滚动/handoff 等副作用回调。
 */
export function useChatStream(deps) {
  const {
    messages,
    sessStore,
    liveThroughput,
    toast,
    reloadMessages,
    friendlyError,
    nowLocalIso,
    stripTail,
    onMaybeScroll,
    onScrollThink,
    onScrollStreamCode,
    sessionMetrics,
    currentTurnMetrics,
    onHandleThreshold,
    onHandoffReady,
    // 宿主当前会话 id 访问器。默认取全局 store；侧边会话实例注入自己的 id，
    // 否则 finishStream 会拿 aside 流的 sid 与全局主会话比较 → sameSession=false
    // → 回复完成后不入列（表现为“AI 回复完不显示内容”）。
    getCurrentSid,
  } = deps
  const currentSid = () => (getCurrentSid ? getCurrentSid() : sessStore.currentSid)

  const generating = ref(false)
  const streamText = ref('')
  const thinkText = ref('')
  const reasoningText = ref('')
  const decisionNotices = ref([])
  const toolEvents = ref([])
  const timeline = ref([])
  let timelineKeySeq = 0

  function pushTimeline(item) {
    timeline.value.push({ _key: `tl-${++timelineKeySeq}`, ...item })
  }

  function resetTimeline() {
    timeline.value = []
    timelineKeySeq = 0
  }

  const thinkOpen = ref(true)
  const preBodyPhase = ref(true)
  const streamSid = ref(null)
  const streamVisuals = ref([])
  const degraded = ref(false)
  const streamPushSuppressed = ref(false)

  let lastCitations = []
  let streamAnalysisMetadata = null

  // ---- 流式正文渲染节流 ----
  let streamChunkBuf = ''
  let streamRaf = 0

  function flushStreamText() {
    if (streamRaf) {
      cancelAnimationFrame(streamRaf)
      streamRaf = 0
    }
    if (!streamChunkBuf) return
    streamText.value += streamChunkBuf
    streamChunkBuf = ''
    onMaybeScroll?.()
    onScrollStreamCode?.()
  }

  // ---- 工具步旁白按步缓冲 ----
  let pendingBody = ''
  let bodyCommitTimer = 0
  let bodyCommitted = false

  function finalizeStaleTimelineSteps() {
    for (const it of timeline.value) {
      if (it.kind === 'memory_stage' && it.status === 'running') it.status = 'ok'
      if (it.kind === 'tool_call' && it.status === 'running') it.status = 'ok'
      if (it.kind === 'step_wait') it.status = 'ok'
    }
  }

  function clearStepWait() {
    if (timeline.value.some((it) => it.kind === 'step_wait')) {
      timeline.value = timeline.value.filter((it) => it.kind !== 'step_wait')
    }
  }

  function upsertStepWait(step, label, detail) {
    const last = timeline.value[timeline.value.length - 1]
    if (last?.kind === 'step_wait' && last.status === 'running') {
      if (step) last.step = step
      if (label) last.label = label
      if (detail) last.detail = detail
      return
    }
    clearStepWait()
    pushTimeline({
      kind: 'step_wait',
      step: step || 0,
      status: 'running',
      label: label || '准备下一步',
      detail: detail || '',
    })
  }

  function pushStreamText(text) {
    if (!text) return
    if (preBodyPhase.value) {
      preBodyPhase.value = false
      thinkOpen.value = false
      finalizeStaleTimelineSteps()
    }
    streamChunkBuf += text
    if (!streamRaf) streamRaf = requestAnimationFrame(flushStreamText)
  }

  function appendTimelineNarration(text) {
    if (!text) return
    const last = timeline.value[timeline.value.length - 1]
    if (last && last.kind === 'narration') {
      last.text = (last.text || '') + text
    } else {
      pushTimeline({ kind: 'narration', text })
    }
  }

  function commitPendingToBody() {
    if (bodyCommitTimer) {
      clearTimeout(bodyCommitTimer)
      bodyCommitTimer = 0
    }
    if (pendingBody) {
      pushStreamText(pendingBody)
      pendingBody = ''
    }
    bodyCommitted = true
  }

  function retractToolStepBody() {
    if (bodyCommitTimer) {
      clearTimeout(bodyCommitTimer)
      bodyCommitTimer = 0
    }
    if (streamRaf) {
      cancelAnimationFrame(streamRaf)
      streamRaf = 0
    }
    const narration = pendingBody + streamText.value + streamChunkBuf
    pendingBody = ''
    streamText.value = ''
    streamChunkBuf = ''
    bodyCommitted = false
    appendTimelineNarration(narration)
    // 工具步旁白撤回后回到「思考/工具」阶段：重新展开时间线；
    // 下一轮正文首字仍会经 pushStreamText 自动折叠。
    preBodyPhase.value = true
    thinkOpen.value = true
  }

  function upsertMemoryStage(data) {
    const stage = data.stage
    if (!stage) return
    for (let i = timeline.value.length - 1; i >= 0; i--) {
      const it = timeline.value[i]
      if (it.kind === 'memory_stage' && it.stage === stage) {
        Object.assign(it, {
          kind: 'memory_stage',
          stage: data.stage,
          status: data.status,
          summary: data.summary,
          candidates: data.candidates,
          hit_count: data.hit_count,
          gate: data.gate,
          refine_path: data.refine_path,
          elapsed_ms: data.elapsed_ms,
          vector_hits: data.vector_hits,
          fts_hits: data.fts_hits,
        })
        if (Array.isArray(data.hits) && data.hits.length) it.hits = data.hits
        return
      }
    }
    pushTimeline({
      kind: 'memory_stage',
      stage: data.stage,
      status: data.status,
      summary: data.summary || '',
      candidates: data.candidates,
      hit_count: data.hit_count,
      gate: data.gate,
      refine_path: data.refine_path,
      elapsed_ms: data.elapsed_ms,
      vector_hits: data.vector_hits,
      fts_hits: data.fts_hits,
      hits: Array.isArray(data.hits) && data.hits.length ? data.hits : undefined,
    })
  }

  function handleEvent(ev, data) {
    if (ev === 'memory_progress') {
      clearStepWait()
      upsertMemoryStage(data)
      onMaybeScroll?.()
      onScrollThink?.()
    } else if (ev === 'reasoning_delta') {
      clearStepWait()
      reasoningText.value += data.text || ''
      const last = timeline.value[timeline.value.length - 1]
      if (last && last.kind === 'reasoning') {
        last.text = (last.text || '') + (data.text || '')
      } else {
        pushTimeline({ kind: 'reasoning', text: data.text || '' })
      }
      liveThroughput.record(data.text)
      onMaybeScroll?.()
      onScrollThink?.()
    } else if (ev === 'content_delta') {
      clearStepWait()
      liveThroughput.record(data.text)
      if (bodyCommitted) {
        pushStreamText(data.text)
      } else {
        pendingBody += data.text
        commitPendingToBody()
      }
    } else if (ev === 'content_reset') {
      retractToolStepBody()
      onMaybeScroll?.()
      onScrollThink?.()
    } else if (ev === 'step_started') {
      upsertStepWait(data.step, '准备下一步')
      onMaybeScroll?.()
      onScrollThink?.()
    } else if (ev === 'step_progress') {
      upsertStepWait(data.step, data.label, data.detail)
      onMaybeScroll?.()
      onScrollThink?.()
    } else if (ev === 'tool_executing') {
      clearStepWait()
      toolEvents.value.push({ type: ev, ...data })
      pushTimeline({
        kind: 'tool_call',
        call_id: data.call_id || '',
        name: data.tool_name || '',
        arguments: data.arguments || '',
        status: 'running',
      })
      onMaybeScroll?.()
      onScrollThink?.()
    } else if (ev === 'tool_result') {
      toolEvents.value.push({ type: ev, ...data })
      for (let i = timeline.value.length - 1; i >= 0; i--) {
        const it = timeline.value[i]
        if (
          it.kind === 'tool_call' &&
          (it.call_id === data.call_id || !data.call_id) &&
          it.name === (data.tool_name || '') &&
          it.status === 'running'
        ) {
          it.status = data.ok ? 'ok' : 'fail'
          if (data.summary) it.result_preview = data.summary.slice(0, 400)
          if (data.citations?.length) it.citations = data.citations
          if (data.error) it.error = String(data.error).slice(0, 400)
          break
        }
      }
      onMaybeScroll?.()
      onScrollThink?.()
    } else if (ev === 'decision_notice') {
      decisionNotices.value.push(data)
      onMaybeScroll?.()
      onScrollThink?.()
    } else if (ev === 'citations') lastCitations = data.refs
    else if (ev === 'queued') toast.push('info', '正在处理上一条消息')
    else if (ev === 'degrade') degraded.value = true
    else if (ev === 'tool_visual') {
      streamVisuals.value.push(data)
      onMaybeScroll?.()
    } else if (ev === 'turn_completed') {
      sessionMetrics.value = data.session_metrics || sessionMetrics.value
      currentTurnMetrics.value = data.metrics || data.session_metrics?.current_turn || null
      liveThroughput.calibrate(currentTurnMetrics.value?.output_tokens)
      streamAnalysisMetadata = data.analysis_metadata || streamAnalysisMetadata
      finishStream(data.message_id)
      if (data.threshold) onHandleThreshold?.(data.threshold)
    } else if (ev === 'step_metrics') {
      sessionMetrics.value = data.session_metrics || sessionMetrics.value
      currentTurnMetrics.value =
        data.metrics || sessionMetrics.value?.current_turn || currentTurnMetrics.value
    } else if (ev === 'error') {
      toast.push('error', friendlyError(data.message))
      finishStream()
    } else if (ev === 'handoff_ready') {
      onHandoffReady?.(data)
    }
  }

  function beginStream(sid) {
    generating.value = true
    streamSid.value = sid
    currentTurnMetrics.value = null
    liveThroughput.reset()
    streamText.value = ''
    thinkText.value = ''
    reasoningText.value = ''
    decisionNotices.value = []
    toolEvents.value = []
    resetTimeline()
    preBodyPhase.value = true
    thinkOpen.value = true
  }

  function finishStream(msgId) {
    commitPendingToBody()
    flushStreamText()
    const finishedSid = streamSid.value
    const sameSession = finishedSid === currentSid()
    if (
      !streamPushSuppressed.value &&
      (streamText.value ||
        thinkText.value ||
        reasoningText.value ||
        decisionNotices.value.length ||
        toolEvents.value.length ||
        timeline.value.length) &&
      sameSession
    ) {
      const body = stripTail(streamText.value, streamVisuals.value)
      messages.value.push({
        id: msgId,
        role: 'assistant',
        content: body || (msgId ? '' : '> ⚠️ 本回复未完成：生成已中断，仅输出了处理进度'),
        citations: lastCitations,
        feedback: 0,
        create_time: nowLocalIso(),
        thinking: thinkText.value || '',
        thinkOpen: false,
        analysis_metadata: streamAnalysisMetadata || {
          schema_version: 'agent-analysis-v1',
          reasoning_text: reasoningText.value,
          system_progress: thinkText.value,
          decision_notices: decisionNotices.value,
          tool_events: toolEvents.value,
          reasoning_available: !!reasoningText.value,
          timeline: [...timeline.value],
        },
        visuals: streamVisuals.value.length ? [...streamVisuals.value] : undefined,
      })
    }
    if (streamRaf) {
      cancelAnimationFrame(streamRaf)
      streamRaf = 0
    }
    streamText.value = ''
    streamChunkBuf = ''
    thinkText.value = ''
    reasoningText.value = ''
    decisionNotices.value = []
    toolEvents.value = []
    resetTimeline()
    preBodyPhase.value = true
    streamVisuals.value = []
    thinkOpen.value = false
    pendingBody = ''
    if (bodyCommitTimer) {
      clearTimeout(bodyCommitTimer)
      bodyCommitTimer = 0
    }
    bodyCommitted = false
    lastCitations = []
    streamAnalysisMetadata = null
    degraded.value = false
    generating.value = false
    streamSid.value = null
    liveThroughput.reset()
    sessStore.scheduleTitleRefresh(finishedSid)
    if (msgId && sameSession && !streamPushSuppressed.value) reloadMessages(currentSid())
    onMaybeScroll?.()
  }

  function cleanupRaf() {
    if (streamRaf) {
      cancelAnimationFrame(streamRaf)
      streamRaf = 0
    }
    // 清理 bodyCommitTimer：流式中途路由切换时 finishStream 不会触发，
    // 避免遗留 pending timer 在新会话里意外提交正文
    if (bodyCommitTimer) {
      clearTimeout(bodyCommitTimer)
      bodyCommitTimer = 0
    }
  }

  const liveHasThinkContent = computed(
    () =>
      thinkText.value ||
      reasoningText.value ||
      decisionNotices.value.length > 0 ||
      toolEvents.value.length > 0 ||
      timeline.value.length > 0
  )
  const showLiveThinkPanel = computed(() => liveHasThinkContent.value)
  const showProcessingPlaceholder = computed(
    () => generating.value && !streamText.value && !liveHasThinkContent.value
  )
  const timelineLive = computed(() => generating.value && preBodyPhase.value)
  const awaitingModel = computed(() =>
    timeline.value.some((it) => it.kind === 'step_wait' && it.status === 'running')
  )
  const showThinkLiveDots = computed(
    () => showLiveThinkPanel.value && (timelineLive.value || awaitingModel.value)
  )
  const liveThinkSummary = computed(() => formatTimelineSummary(timeline.value))

  return {
    generating,
    streamText,
    thinkText,
    reasoningText,
    decisionNotices,
    toolEvents,
    timeline,
    thinkOpen,
    preBodyPhase,
    streamSid,
    streamVisuals,
    degraded,
    streamPushSuppressed,
    liveHasThinkContent,
    showLiveThinkPanel,
    showProcessingPlaceholder,
    timelineLive,
    awaitingModel,
    showThinkLiveDots,
    liveThinkSummary,
    beginStream,
    handleEvent,
    finishStream,
    cleanupRaf,
    resetTimeline,
  }
}
