import React, { useEffect, useMemo, useRef, useState } from 'react';
import { Authenticator } from '@aws-amplify/ui-react';
import { uploadData, getUrl, list } from 'aws-amplify/storage';
import ReactMarkdown from 'react-markdown';
import '@aws-amplify/ui-react/styles.css';
import './App.css';

import {
  getConversation,
  isApiConfigured,
  listConversations,
  sendChatMessage,
  streamChatMessage,
} from './api';
import { displayAnalysisLabel, displayMetricLabel } from './metricLabels';

const CHAT_PROVENANCE_LABELS = {
  data_tools: 'From data/tools',
  data_tools_with_guidance: 'Data + guidance',
  general_interpretation: 'General interpretation',
};

function formatPrimitive(value) {
  if (value === null || value === undefined || value === '') {
    return 'N/A';
  }
  if (typeof value === 'number') {
    return Number.isInteger(value) ? value.toLocaleString() : value.toFixed(2);
  }
  return String(value);
}

function createIntroMessage() {
  return {
    id: 'intro',
    role: 'assistant',
    text: isApiConfigured()
      ? 'Ask about the dataset, IV, open interest, positioning, or broader market context. I use data and tools when numbers matter, and I will say when an answer is general interpretation rather than from your files.'
      : 'Set `REACT_APP_API_BASE_URL` in Amplify to enable the analytics assistant.',
  };
}

function normalizeConversationMessage(message) {
  return {
    id: message.id || `${message.role}-${message.created_at || Math.random()}`,
    role: message.role,
    text: message.text || '',
    provenance: message.provenance,
    asOfDate: message.as_of_date,
    data: message.data,
    toolTrace: message.tool_trace || [],
    sources: message.sources || [],
    responseMode: message.response_mode,
    elapsedMs: message.elapsed_ms,
    promptVersion: message.prompt_version,
    isStreaming: false,
  };
}

function buildAssistantFromApiResponse(response, textOverride, idOverride) {
  const text = textOverride !== undefined ? textOverride : (response.answer || '');
  return {
    id: idOverride || `assistant-${Date.now()}`,
    role: 'assistant',
    text,
    provenance: response.provenance,
    asOfDate: response.as_of_date,
    data: response.data,
    toolTrace: response.tool_trace || [],
    sources: response.sources || [],
    responseMode: response.response_mode,
    elapsedMs: response.elapsed_ms,
    promptVersion: response.prompt_version,
    isStreaming: false,
  };
}

function DataHighlights({ data }) {
  if (!data) {
    return null;
  }

  const items = [];
  if (data.metric) {
    const label = data.metric_label || displayMetricLabel(data.metric);
    items.push(`Metric: ${label}`);
  }
  if (data.analysis_type) {
    const label = data.analysis_label || displayAnalysisLabel(data.analysis_type);
    items.push(`Analysis: ${label}`);
  }
  if (data.window_days) {
    items.push(`Window: ${data.window_days} days`);
  }
  if (data.latest_value !== undefined) {
    if (
      formatPrimitive(data.latest_value) === 'N/A' &&
      data.latest_observed_value !== undefined &&
      data.latest_observed_value !== null &&
      data.latest_observed_date
    ) {
      items.push(`Latest (as of ${data.end_date || 'end date'}): N/A`);
      items.push(`Last available: ${formatPrimitive(data.latest_observed_value)} on ${data.latest_observed_date}`);
    } else {
      items.push(`Latest: ${formatPrimitive(data.latest_value)}`);
    }
  }
  if (data.average_value !== undefined && data.average_value !== null) {
    items.push(`Average: ${formatPrimitive(data.average_value)}`);
  }
  if (Array.isArray(data.series)) {
    items.push(`Points: ${data.series.length}`);
  }
  if (Array.isArray(data.contracts)) {
    items.push(`Contracts: ${data.contracts.length}`);
  }
  if (data.primary_date && data.secondary_date) {
    items.push(`Compared: ${data.primary_date} vs ${data.secondary_date}`);
  }
  if (data.start_date && data.end_date) {
    items.push(`Range: ${data.start_date} to ${data.end_date}`);
  }

  if (items.length === 0) {
    return null;
  }

  return (
    <div className="highlight-row">
      {items.map((item) => (
        <span key={item} className="highlight-pill">{item}</span>
      ))}
    </div>
  );
}

function TraceDetails({ toolTrace, sources }) {
  if ((!toolTrace || toolTrace.length === 0) && (!sources || sources.length === 0)) {
    return null;
  }

  return (
    <div className="trace-details">
      {toolTrace && toolTrace.length > 0 ? (
        <div className="trace-section">
          <p className="trace-title">Tool trace</p>
          {toolTrace.map((trace, index) => (
            <div key={`${trace.tool}-${index}`} className="trace-card">
              <p><strong>Tool:</strong> {trace.tool}</p>
              {trace.confidence ? <p><strong>Confidence:</strong> {trace.confidence}</p> : null}
              {trace.reasoning ? <p><strong>Planner note:</strong> {trace.reasoning}</p> : null}
              {trace.args ? (
                <pre className="trace-pre">{JSON.stringify(trace.args, null, 2)}</pre>
              ) : null}
            </div>
          ))}
        </div>
      ) : null}
      {sources && sources.length > 0 ? (
        <div className="trace-section">
          <p className="trace-title">Retrieved guidance</p>
          {sources.map((source) => (
            <div key={source.id} className="trace-card">
              <p><strong>{source.title}</strong></p>
              <p>{source.source_type} · {source.topic} · trust {source.trust_level}</p>
              <p>{source.excerpt}</p>
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function ConversationSidebar({
  conversations,
  activeConversationId,
  onSelect,
  onNewChat,
}) {
  return (
    <aside className="chat-sidebar">
      <div className="chat-sidebar-header">
        <div>
          <h2>Chats</h2>
          <p className="muted-text">Resume or start fresh.</p>
        </div>
        <button className="primary-button sidebar-new" onClick={onNewChat} type="button">
          New
        </button>
      </div>
      <div className="conversation-list">
        {conversations.length === 0 ? (
          <p className="muted-text">No saved threads yet.</p>
        ) : (
          conversations.map((c) => (
            <button
              key={c.conversation_id}
              type="button"
              className={`conversation-item ${c.conversation_id === activeConversationId ? 'active' : ''}`}
              onClick={() => onSelect(c.conversation_id)}
            >
              <span className="conversation-title">{c.title}</span>
              <span className="conversation-preview">{c.last_message_preview || '—'}</span>
            </button>
          ))
        )}
      </div>
    </aside>
  );
}

function ChatBubble({ message, isSelected, onToggleInspect }) {
  const isAssistant = message.role === 'assistant';
  const showInspect = isAssistant && !message.isStreaming;

  return (
    <div className={`chat-message ${isAssistant ? 'assistant' : 'user'} ${isSelected ? 'selected' : ''}`}>
      <div className="chat-bubble">
        <div className="chat-body markdown-body">
          {message.isStreaming && !message.text ? (
            <span className="streaming-dots">Thinking…</span>
          ) : (
            <ReactMarkdown>{message.text || ''}</ReactMarkdown>
          )}
        </div>
        {isAssistant && !message.isStreaming ? <DataHighlights data={message.data} /> : null}
        {isAssistant && !message.isStreaming && (message.provenance || message.asOfDate) ? (
          <div className="chat-meta">
            {message.provenance ? (
              <span className="chat-pill">
                {CHAT_PROVENANCE_LABELS[message.provenance] || message.provenance}
              </span>
            ) : null}
            {message.asOfDate ? <span>As of {message.asOfDate}</span> : null}
          </div>
        ) : null}
        {showInspect ? (
          <div className="chat-actions">
            <button
              className="secondary-button"
              type="button"
              onClick={() => onToggleInspect(message.id)}
            >
              {isSelected ? 'Hide details' : 'Details'}
            </button>
          </div>
        ) : null}
      </div>
    </div>
  );
}

function InsightRail({ message, onClose }) {
  if (!message) {
    return (
      <aside className="insight-rail empty">
        <h3>Details</h3>
        <p className="muted-text">Select an assistant message and open Details for tool trace and retrieved guidance.</p>
      </aside>
    );
  }

  return (
    <aside className="insight-rail">
      <div className="insight-rail-header">
        <div>
          <h3>Grounding</h3>
          <p className="muted-text">
            {CHAT_PROVENANCE_LABELS[message.provenance] || message.provenance || 'Assistant'}
          </p>
        </div>
        <button type="button" className="text-button" onClick={onClose}>Close</button>
      </div>
      {message.promptVersion ? (
        <p className="prompt-version">Prompt: {message.promptVersion}</p>
      ) : null}
      <DataHighlights data={message.data} />
      <TraceDetails toolTrace={message.toolTrace} sources={message.sources} />
    </aside>
  );
}

function Dashboard({ user, signOut }) {
  const [selectedFiles, setSelectedFiles] = useState([]);
  const [uploadStatus, setUploadStatus] = useState('');
  const [outputFileDate, setOutputFileDate] = useState('');
  const [downloadUrl, setDownloadUrl] = useState('');
  const [isChecking, setIsChecking] = useState(false);
  const [pastReports, setPastReports] = useState([]);
  const [isLoadingReports, setIsLoadingReports] = useState(true);
  const [chatInput, setChatInput] = useState('');
  const [chatMessages, setChatMessages] = useState(() => [createIntroMessage()]);
  const [chatError, setChatError] = useState('');
  const [chatProgress, setChatProgress] = useState('');
  const [isSendingChat, setIsSendingChat] = useState(false);
  const [conversations, setConversations] = useState([]);
  const [activeConversationId, setActiveConversationId] = useState(null);
  const [conversationTitle, setConversationTitle] = useState('New chat');
  const [detailMessageId, setDetailMessageId] = useState(null);
  const threadRef = useRef(null);

  useEffect(() => {
    fetchPastReports();
    if (isApiConfigured()) {
      refreshConversations();
    }
  }, []);

  useEffect(() => {
    const el = threadRef.current;
    if (el) {
      el.scrollTop = el.scrollHeight;
    }
  }, [chatMessages]);

  const detailMessage = useMemo(
    () => chatMessages.find((m) => m.id === detailMessageId) || null,
    [chatMessages, detailMessageId],
  );

  const fetchPastReports = async () => {
    try {
      setIsLoadingReports(true);
      const result = await list({
        path: 'optionsai/runs/default-run/outputs/',
      });
      const docxFiles = result.items.filter((item) => item.path.endsWith('.docx') && !item.path.includes('~$'));
      docxFiles.sort((a, b) => new Date(b.lastModified) - new Date(a.lastModified));
      setPastReports(docxFiles);
    } catch (error) {
      console.error('Error fetching past reports:', error);
    } finally {
      setIsLoadingReports(false);
    }
  };

  const refreshConversations = async () => {
    try {
      const items = await listConversations();
      setConversations(items);
    } catch (error) {
      console.error('Error listing conversations:', error);
    }
  };

  const openConversation = async (conversationId) => {
    try {
      const conv = await getConversation(conversationId);
      setActiveConversationId(conv.conversation_id);
      setConversationTitle(conv.title);
      setDetailMessageId(null);
      setChatMessages(
        conv.messages && conv.messages.length > 0
          ? conv.messages.map(normalizeConversationMessage)
          : [createIntroMessage()],
      );
    } catch (error) {
      setChatError(error.message);
    }
  };

  const handleNewChat = () => {
    setActiveConversationId(null);
    setConversationTitle('New chat');
    setChatMessages([createIntroMessage()]);
    setDetailMessageId(null);
    setChatError('');
  };

  const handleFileSelection = (event) => {
    const newFiles = Array.from(event.target.files);
    setSelectedFiles((prevFiles) => {
      const existingFileNames = new Set(prevFiles.map((f) => f.name));
      const uniqueNewFiles = newFiles.filter((f) => !existingFileNames.has(f.name));
      return [...prevFiles, ...uniqueNewFiles];
    });
    setUploadStatus('');
    setDownloadUrl('');
    event.target.value = '';
  };

  const removeFile = (fileNameToRemove) => {
    setSelectedFiles((prevFiles) => prevFiles.filter((file) => file.name !== fileNameToRemove));
  };

  const handleBulkUpload = async () => {
    if (selectedFiles.length === 0) return;
    setUploadStatus(`Uploading ${selectedFiles.length} files to S3...`);
    const dateMatch = selectedFiles[0].name.match(/(\d{4}_\d{2}_\d{2})/);
    if (dateMatch) {
      setOutputFileDate(dateMatch[0].replace(/_/g, '-'));
    }
    try {
      await Promise.all(
        selectedFiles.map((file) =>
          uploadData({
            path: `optionsai/Inputs/pdfs/${file.name}`,
            data: file,
          }).result,
        ),
      );
      setUploadStatus(`Success: ${selectedFiles.length} file(s) uploaded; pipeline triggered.`);
      setSelectedFiles([]);
    } catch (error) {
      console.error('Error uploading files: ', error);
      setUploadStatus(`Error: ${error.message}`);
    }
  };

  const checkForReport = async () => {
    if (!outputFileDate) return;
    setIsChecking(true);
    setUploadStatus('Checking S3 for the final report...');
    try {
      const result = await getUrl({
        path: `optionsai/runs/default-run/outputs/${outputFileDate}.docx`,
        options: { validateObjectExistence: true },
      });
      setDownloadUrl(result.url.toString());
      setUploadStatus('Report is ready.');
      fetchPastReports();
    } catch (error) {
      setUploadStatus('Still processing… check back shortly.');
    } finally {
      setIsChecking(false);
    }
  };

  const downloadSpecificReport = async (path) => {
    try {
      const result = await getUrl({ path });
      window.open(result.url.toString(), '_blank');
    } catch (error) {
      console.error('Error downloading specific report:', error);
      alert('Could not download the report. Please try again.');
    }
  };

  const handleChatSubmit = async (event) => {
    event.preventDefault();
    const message = chatInput.trim();
    if (!message || isSendingChat || !isApiConfigured()) {
      return;
    }

    setChatError('');
    setChatProgress('');
    setIsSendingChat(true);
    setChatInput('');

    const userMessage = { id: `user-${Date.now()}`, role: 'user', text: message };
    const pendingId = `assistant-pending-${Date.now()}`;
    setChatMessages((prev) => [
      ...prev.filter((m) => m.id !== 'intro'),
      userMessage,
      {
        id: pendingId,
        role: 'assistant',
        text: '',
        isStreaming: true,
        toolTrace: [],
        sources: [],
      },
    ]);

    const applyDone = (meta, accumulatedText) => {
      setActiveConversationId(meta.conversation_id);
      if (meta.conversation_title) {
        setConversationTitle(meta.conversation_title);
      }
      setChatMessages((prev) =>
        prev.map((m) =>
          m.id === pendingId
            ? buildAssistantFromApiResponse(
                { ...meta, answer: accumulatedText ?? m.text },
                undefined,
                pendingId,
              )
            : m,
        ),
      );
      setDetailMessageId(pendingId);
      setChatProgress('');
      refreshConversations();
    };

    try {
      let accumulated = '';
      await streamChatMessage(message, activeConversationId, {
        onChunk: (chunk) => {
          accumulated += chunk;
          setChatMessages((prev) =>
            prev.map((m) => (m.id === pendingId ? { ...m, text: accumulated } : m)),
          );
        },
        onProgress: (event) => {
          void event;
          setChatProgress('Thinking...');
        },
        onHeartbeat: (event) => {
          void event;
          setChatProgress('Thinking...');
        },
        onDone: (meta) => {
          applyDone(meta, accumulated);
        },
      });
    } catch (streamError) {
      try {
        const response = await sendChatMessage(message, activeConversationId);
        applyDone(response, response.answer);
      } catch (fallbackError) {
        setChatError(fallbackError.message);
        setChatProgress('');
        setChatMessages((prev) =>
          prev.map((m) =>
            m.id === pendingId
              ? {
                  ...m,
                  text: fallbackError.message || 'Request failed.',
                  isStreaming: false,
                }
              : m,
          ),
        );
      }
    } finally {
      setIsSendingChat(false);
    }
  };

  const toggleInspect = (id) => {
    setDetailMessageId((cur) => (cur === id ? null : id));
  };

  return (
    <div className="dashboard-shell">
      <div className="dashboard-header">
        <div>
          <h1>OptionsAI</h1>
          <p>
            Signed in as <strong>{user?.signInDetails?.loginId || user?.username}</strong>
          </p>
        </div>
        <button className="danger-button" type="button" onClick={signOut}>
          Sign out
        </button>
      </div>

      <div className="workspace-grid">
        <ConversationSidebar
          conversations={conversations}
          activeConversationId={activeConversationId}
          onSelect={openConversation}
          onNewChat={handleNewChat}
        />

        <section className="panel chat-workspace">
          <div className="panel-heading">
            <div>
              <h2>{conversationTitle}</h2>
              <p className="muted-text">
                Natural chat with data-grounded tools. Broader takes are labeled when they are not from the dataset.
              </p>
            </div>
            <span className={`chat-availability ${isApiConfigured() ? 'ready' : 'disabled'}`}>
              {isApiConfigured() ? 'API' : 'No API URL'}
            </span>
          </div>

          <div className="chat-thread" ref={threadRef}>
            {chatMessages.map((m) => (
              <ChatBubble
                key={m.id}
                message={m}
                isSelected={detailMessageId === m.id}
                onToggleInspect={toggleInspect}
              />
            ))}
          </div>

          {chatError ? <p className="status-text error">{chatError}</p> : null}
          {!chatError && chatProgress ? <p className="status-text">{chatProgress}</p> : null}

          <form className="chat-form" onSubmit={handleChatSubmit}>
            <textarea
              className="chat-input"
              value={chatInput}
              onChange={(e) => setChatInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key !== 'Enter' || e.shiftKey) {
                  return;
                }
                e.preventDefault();
                if (!chatInput.trim() || !isApiConfigured() || isSendingChat) {
                  return;
                }
                e.currentTarget.form?.requestSubmit();
              }}
              placeholder="Follow up naturally, ask for IV, OI interpretation, or comparisons."
              rows={3}
              disabled={!isApiConfigured() || isSendingChat}
            />
            <div className="chat-form-actions">
              <button type="button" className="secondary-button" onClick={handleNewChat}>
                New chat
              </button>
              <button
                type="submit"
                className="primary-button"
                disabled={!chatInput.trim() || !isApiConfigured() || isSendingChat}
              >
                {isSendingChat ? 'Sending…' : 'Send'}
              </button>
            </div>
          </form>
        </section>

        <InsightRail
          message={detailMessage}
          onClose={() => setDetailMessageId(null)}
        />
      </div>

      <div className="dashboard-grid utilities-row">
        <div className="panel utility-panel">
          <div className="panel-heading">
            <div>
              <h2>Upload daily PDFs</h2>
              <p>Select KC1 (CSO) PDFs to run the pipeline.</p>
            </div>
          </div>
          <input type="file" accept="application/pdf" multiple onChange={handleFileSelection} />
          {selectedFiles.length > 0 ? (
            <div className="selected-files">
              <p className="selected-files-title">{selectedFiles.length} file(s) queued</p>
              <ul className="selected-files-list">
                {selectedFiles.map((file, index) => (
                  <li key={index} className="selected-file-item">
                    <span>{file.name}</span>
                    <button type="button" className="text-button" onClick={() => removeFile(file.name)}>
                      Remove
                    </button>
                  </li>
                ))}
              </ul>
              <button type="button" className="primary-button full-width" onClick={handleBulkUpload}>
                Upload and run
              </button>
            </div>
          ) : null}
          <p className={`status-text ${uploadStatus.includes('Error') ? 'error' : 'success'}`}>
            {uploadStatus}
          </p>
          {outputFileDate ? (
            <div className="subsection">
              <h3>Latest report</h3>
              {!downloadUrl ? (
                <button type="button" className="secondary-button" onClick={checkForReport} disabled={isChecking}>
                  {isChecking ? 'Checking…' : 'Check if ready'}
                </button>
              ) : (
                <a className="primary-link" href={downloadUrl} target="_blank" rel="noopener noreferrer">
                  Download {outputFileDate}.docx
                </a>
              )}
            </div>
          ) : null}
        </div>

        <div className="panel utility-panel">
          <div className="panel-heading">
            <div>
              <h2>Tips</h2>
              <p>Short follow-ups work: “same for futures OI?” or “what does that combo usually mean?”</p>
            </div>
          </div>
        </div>
      </div>

      <div className="panel archive-panel">
        <div className="panel-heading">
          <div>
            <h2>Past reports</h2>
            <p>Download generated `.docx` files from S3.</p>
          </div>
          <button type="button" className="secondary-button" onClick={fetchPastReports}>
            Refresh
          </button>
        </div>
        {isLoadingReports ? (
          <p>Loading…</p>
        ) : pastReports.length === 0 ? (
          <p className="muted-text">No reports found.</p>
        ) : (
          <ul className="archive-list">
            {pastReports.map((report, index) => {
              const fileName = report.path.split('/').pop();
              return (
                <li key={index} className="archive-item">
                  <span>{fileName}</span>
                  <button type="button" className="secondary-button" onClick={() => downloadSpecificReport(report.path)}>
                    Download
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </div>
  );
}

export default function App() {
  return (
    <Authenticator>
      {({ signOut, user }) => <Dashboard user={user} signOut={signOut} />}
    </Authenticator>
  );
}
