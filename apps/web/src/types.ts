export type DocumentItem = {
  id: string;
  title: string;
  status: "processing" | "ready" | "partial" | "failed";
  active_version_id?: string;
  job_id?: string;
  current_stage?: string;
  progress?: number;
  slide_count?: number;
  review_count?: number;
  updated_at: string;
};

export type SlideItem = {
  id: string;
  slide_no: number;
  title?: string;
  summary?: string;
  quality_score: number;
  preview_url: string;
  thumbnail_url?: string;
  element_count?: number;
};

export type ElementItem = {
  id: string;
  element_type: string;
  reading_order: number;
  bbox: { x: number; y: number; w: number; h: number };
  text_content?: string;
  structured: Record<string, unknown>;
  provenance: Record<string, unknown>;
  confidence: number;
};

export type ChartPoint = {
  id: string;
  category?: string;
  y_value?: number;
  display_value?: string;
  confidence: number;
};

export type ChartSeries = {
  id: string;
  name?: string;
  unit?: string;
  chart_type?: string;
  points: ChartPoint[];
};

export type Chart = {
  id: string;
  element_id: string;
  title?: string;
  source_kind: string;
  confidence: number;
  chart_types: string[];
  series: ChartSeries[];
  warnings: Array<Record<string, unknown>>;
};

export type SlideDetail = SlideItem & {
  document_title: string;
  elements: ElementItem[];
  charts: Chart[];
  notes_text?: string;
};

export type Citation = {
  id: string;
  label: string;
  document_title: string;
  slide_id: string;
  slide_no: number;
  element_id?: string;
  element_type?: string;
  quote: string;
  bbox: { x: number; y: number; w: number; h: number };
  confidence: number;
  source_kind: string;
  preview_url: string;
};

export type Message = {
  id: string;
  role: "user" | "assistant";
  content: string;
  status: string;
  citations: Citation[];
  metadata?: {
    answer_mode?: string;
    show_visuals?: boolean;
    knowledge_source?: string;
  };
};

export type Conversation = {
  id: string;
  title: string;
  scope: { document_ids: string[] };
  messages: Message[];
};

export type ConversationSummary = {
  id: string;
  title: string;
  scope: { document_ids: string[] };
  message_count: number;
  last_question?: string;
  last_activity_at: string;
  created_at: string;
};
