import { FormEvent, PointerEvent, useEffect, useMemo, useRef, useState } from 'react';
import {
  APP_STATE_KEY,
  CARD_POOL,
  type AppState,
  type BocCard,
  type ChatMessage,
  type Rarity,
  type VerticalKey,
  getVerticalByKey,
  starterCardIds,
  verticals,
} from './cards';

const BACKEND_URL = import.meta.env.VITE_BACKEND_URL ?? (import.meta.env.DEV ? '' : 'http://localhost:8000');

type AskResponse = {
  answer: string;
  sources: string[];
  verticale: VerticalKey;
};

type CardDecisionResponse = {
  action: 'created' | 'already_owned' | 'skipped';
  card?: BocCard | null;
  existingCardId?: string | null;
  message: string;
};

type Route =
  | { name: 'home' }
  | { name: 'vertical'; id: number }
  | { name: 'share'; cardId: string };

const initialState: AppState = {
  collectedCardIds: starterCardIds,
  chatHistory: [],
  firstVisit: true,
  discoveredCards: [],
};

const cardImageFallbacks: Record<VerticalKey, { url: string; alt: string }> = {
  relocation: {
    url: 'https://www.unibocconi.it/sites/default/files/styles/fullwidth_xxl/public/media/images/piazza_gae_aulenti_0.jpg.webp?itok=m2__eciS',
    alt: 'Piazza Gae Aulenti in Milan',
  },
  life_on_campus: {
    url: 'https://www.unibocconi.it/sites/default/files/styles/link_card/public/media/images/studenti_4.jpg.webp?itok=gdH9KVpy',
    alt: 'Bocconi students gathered on campus',
  },
  study_abroad: {
    url: 'https://www.unibocconi.it/sites/default/files/styles/fullwidth_xxl/public/media/images/_mg_7245_0_0.jpg.webp?itok=bsdBRWTs',
    alt: 'Bocconi international mobility students',
  },
  career_readiness: {
    url: 'https://www.unibocconi.it/sites/default/files/styles/highlight_slide/public/media/images/ipp-402.jpg.webp?itok=q3qk9oCr',
    alt: 'BocconiJobs career event',
  },
};

function readAppState(): AppState {
  try {
    const raw = localStorage.getItem(APP_STATE_KEY);
    if (!raw) return initialState;
    const parsed = JSON.parse(raw) as Partial<AppState>;
    return {
      collectedCardIds: parsed.collectedCardIds?.length
        ? parsed.collectedCardIds
        : starterCardIds,
      chatHistory: parsed.chatHistory ?? [],
      firstVisit: parsed.firstVisit ?? false,
      discoveredCards: parsed.discoveredCards ?? [],
    };
  } catch {
    return initialState;
  }
}

function parseRoute(): Route {
  const [, section, value] = window.location.pathname.split('/');
  if (section === 'vertical') {
    const id = Number(value);
    return verticals.some((vertical) => vertical.id === id)
      ? { name: 'vertical', id }
      : { name: 'home' };
  }
  if (section === 'share' && value) return { name: 'share', cardId: value };
  return { name: 'home' };
}

function App() {
  const [route, setRoute] = useState<Route>(parseRoute);
  const [state, setState] = useState<AppState>(readAppState);
  const [chatOpen, setChatOpen] = useState(false);
  const [activeCardId, setActiveCardId] = useState<string | null>(null);
  const [rarityFilter, setRarityFilter] = useState<Rarity | 'all'>('all');
  const [sortMode, setSortMode] = useState<'newest' | 'oldest' | 'rarity'>('newest');
  const [showSplash, setShowSplash] = useState(state.firstVisit);

  const cards = useMemo(
    () => [...CARD_POOL, ...state.discoveredCards],
    [state.discoveredCards],
  );
  const collectedSet = useMemo(
    () => new Set(state.collectedCardIds),
    [state.collectedCardIds],
  );
  const activeCard = activeCardId
    ? cards.find((card) => card.id === activeCardId) ?? null
    : null;

  const withTransition = (update: () => void) => {
    if ('startViewTransition' in document) {
      (document as Document & { startViewTransition: (callback: () => void) => void })
        .startViewTransition(update);
      return;
    }
    update();
  };

  useEffect(() => {
    localStorage.setItem(APP_STATE_KEY, JSON.stringify(state));
  }, [state]);

  useEffect(() => {
    const onPop = () => setRoute(parseRoute());
    window.addEventListener('popstate', onPop);
    return () => window.removeEventListener('popstate', onPop);
  }, []);

  useEffect(() => {
    if (!showSplash) return;
    const timeout = window.setTimeout(() => {
      setShowSplash(false);
      setState((current) => ({ ...current, firstVisit: false }));
    }, 1900);
    return () => window.clearTimeout(timeout);
  }, [showSplash]);

  const navigate = (nextRoute: Route) => {
    const path =
      nextRoute.name === 'vertical'
        ? `/vertical/${nextRoute.id}`
        : nextRoute.name === 'share'
          ? `/share/${nextRoute.cardId}`
          : '/';
    withTransition(() => {
      window.history.pushState({}, '', path);
      setRoute(nextRoute);
      setActiveCardId(null);
    });
  };

  const openCard = (id: string) => {
    withTransition(() => setActiveCardId(id));
  };

  const addChatMessage = (message: ChatMessage) => {
    setState((current) => ({
      ...current,
      chatHistory: [...current.chatHistory, message].slice(-50),
    }));
  };

  const clearChatHistory = () => {
    setState((current) => ({
      ...current,
      chatHistory: [],
    }));
  };

  const applyCardDecision = async (question: string, answer: AskResponse) => {
    const response = await fetch(`${BACKEND_URL}/cards/decision`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        question,
        answer: answer.answer,
        sources: answer.sources,
        verticale: answer.verticale,
        cards,
        collectedCardIds: Array.from(collectedSet),
      }),
    });
    const payload = await readJsonResponse(
      response,
      'The card collection endpoint returned an empty response.',
      'The card collection endpoint returned a response the app could not read.',
    );
    if (!response.ok || !isCardDecisionResponse(payload)) {
      throw new Error('The card collection endpoint is not ready yet.');
    }

    if (payload.action === 'created' && payload.card) {
      const newCard = payload.card;
      setState((current) => {
        if (current.collectedCardIds.includes(newCard.id)) return current;
        return {
          ...current,
          discoveredCards: [newCard, ...current.discoveredCards],
          collectedCardIds: [newCard.id, ...current.collectedCardIds],
        };
      });
      setRevealCard(newCard);
      withTransition(() => setActiveCardId(newCard.id));
      addChatMessage({
        id: crypto.randomUUID(),
        role: 'assistant',
        text: payload.message,
        createdAt: new Date().toISOString(),
        vertical: newCard.vertical,
        card: newCard,
      });
      window.setTimeout(() => setRevealCard(null), 2800);
      return;
    }

    if (payload.action === 'already_owned') {
      if (payload.existingCardId) {
        openCard(payload.existingCardId);
      }
      addChatMessage({
        id: crypto.randomUUID(),
        role: 'assistant',
        text: payload.message,
        createdAt: new Date().toISOString(),
        vertical: answer.verticale,
      });
    }
  };

  const onAsk = async (question: string) => {
    const trimmed = question.trim();
    if (!trimmed) return;

    addChatMessage({
      id: crypto.randomUUID(),
      role: 'user',
      text: trimmed,
      createdAt: new Date().toISOString(),
    });

    try {
      const response = await fetch(`${BACKEND_URL}/ask`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question: trimmed }),
      });
      const payload = await readJsonResponse(
        response,
        'The AI endpoint returned an empty response. Try again in a moment.',
        'The AI endpoint returned a response the app could not read.',
      );
      if (!response.ok || !isAskResponse(payload)) {
        const detail =
          getResponseDetail(payload) ?? 'The AI endpoint is not ready yet.';
        throw new Error(detail);
      }
      const answer = payload;

      addChatMessage({
        id: crypto.randomUUID(),
        role: 'assistant',
        text: answer.answer,
        createdAt: new Date().toISOString(),
        sources: answer.sources,
        vertical: answer.verticale,
      });

      try {
        await applyCardDecision(trimmed, answer);
      } catch {
        const fallbackCard = createFallbackCard(trimmed, answer, cards);
        setState((current) => {
          if (current.collectedCardIds.includes(fallbackCard.id)) return current;
          return {
            ...current,
            discoveredCards: [fallbackCard, ...current.discoveredCards],
            collectedCardIds: [fallbackCard.id, ...current.collectedCardIds],
          };
        });
        withTransition(() => setActiveCardId(fallbackCard.id));
        addChatMessage({
          id: crypto.randomUUID(),
          role: 'assistant',
          text: `New card created: ${fallbackCard.title}.`,
          createdAt: new Date().toISOString(),
          vertical: answer.verticale,
          card: fallbackCard,
        });
      }
    } catch (error) {
      addChatMessage({
        id: crypto.randomUUID(),
        role: 'assistant',
        text:
          error instanceof Error
            ? error.message
            : "I couldn't reach the AI buddy right now. Try again in a moment.",
        createdAt: new Date().toISOString(),
      });
    }
  };

  const page =
    route.name === 'vertical' ? (
      <CollectionPage
        verticalId={route.id}
        cards={cards}
        collectedSet={collectedSet}
        rarityFilter={rarityFilter}
        sortMode={sortMode}
        onBack={() => navigate({ name: 'home' })}
        onCardOpen={openCard}
        onFilterChange={setRarityFilter}
        onSortChange={setSortMode}
      />
    ) : route.name === 'share' ? (
      <SharePage
        card={cards.find((card) => card.id === route.cardId) ?? null}
        onHome={() => navigate({ name: 'home' })}
      />
    ) : (
      <HomePage
        cards={cards}
        collectedSet={collectedSet}
        onVerticalOpen={(id) => navigate({ name: 'vertical', id })}
        onCardOpen={openCard}
      />
    );

  return (
    <div className="app-shell">
      {showSplash && <Splash />}
      <DesktopNav
        collected={state.collectedCardIds.length}
        route={route}
        onHome={() => navigate({ name: 'home' })}
        onVerticalOpen={(id) => navigate({ name: 'vertical', id })}
      />
      {page}
      <MobileNav route={route} onVerticalOpen={(id) => navigate({ name: 'vertical', id })} />
      <button
        className="chat-fab"
        type="button"
        aria-label="Open boccodex to ask a question"
        aria-expanded={chatOpen}
        onClick={() => setChatOpen(true)}
      >
        <i className="ph-duotone ph-sparkle" />
      </button>
      <ChatPanel
        open={chatOpen}
        history={state.chatHistory}
        onAsk={onAsk}
        onCardOpen={openCard}
        onClear={clearChatHistory}
        onClose={() => setChatOpen(false)}
      />
      {activeCard && (
        <CardDetailModal
          card={activeCard}
          related={cards.filter(
            (card) => card.vertical === activeCard.vertical && card.id !== activeCard.id,
          )}
          onClose={() => withTransition(() => setActiveCardId(null))}
          onShareRoute={() => navigate({ name: 'share', cardId: activeCard.id })}
        />
      )}
    </div>
  );
}

function Splash() {
  return (
    <div className="splash" aria-hidden="true">
      <div className="splash-logo">B</div>
      <h1>boccodex</h1>
      <p>Your Bocconi. One card at a time.</p>
    </div>
  );
}

function DesktopNav({
  collected,
  route,
  onHome,
  onVerticalOpen,
}: {
  collected: number;
  route: Route;
  onHome: () => void;
  onVerticalOpen: (id: number) => void;
}) {
  return (
    <header className="desktop-nav">
      <button className="brand-button" type="button" onClick={onHome}>
        <span className="brand-mark">B</span>
        <span>
          <strong>boccodex</strong>
          <small>Campus card console</small>
        </span>
      </button>
      <nav aria-label="Vertical navigation">
        {verticals.map((vertical) => (
          <button
            className={
              route.name === 'vertical' && route.id === vertical.id ? 'active' : undefined
            }
            type="button"
            key={vertical.key}
            onClick={() => onVerticalOpen(vertical.id)}
          >
            <i className={vertical.icon} />
            {vertical.navLabel}
          </button>
        ))}
      </nav>
      <div className="nav-counter">{collected} cards</div>
    </header>
  );
}

function MobileNav({
  route,
  onVerticalOpen,
}: {
  route: Route;
  onVerticalOpen: (id: number) => void;
}) {
  return (
    <nav className="mobile-nav" aria-label="Vertical navigation">
      {verticals.map((vertical) => (
        <button
          className={route.name === 'vertical' && route.id === vertical.id ? 'active' : undefined}
          type="button"
          key={vertical.key}
          onClick={() => onVerticalOpen(vertical.id)}
        >
          <i className={vertical.icon} />
          <span>{vertical.mobileLabel}</span>
        </button>
      ))}
    </nav>
  );
}

function HomePage({
  cards,
  collectedSet,
  onVerticalOpen,
  onCardOpen,
}: {
  cards: BocCard[];
  collectedSet: Set<string>;
  onVerticalOpen: (id: number) => void;
  onCardOpen: (id: string) => void;
}) {
  return (
    <main className="home-page">
      <section className="home-intro">
        <p className="eyebrow">Bocconi starter deck</p>
        <h1>boccodex</h1>
        <p>
          Ask about Milan life, campus services, exchanges, and careers. Each useful
          answer can become a new card in your collection.
        </p>
      </section>
      <section className="wallet-grid" aria-label="Card wallets">
        {verticals.map((vertical, index) => {
          const verticalCards = cards.filter((card) => card.vertical === vertical.key);
          const collected = verticalCards.filter((card) => collectedSet.has(card.id));
          return (
            <Wallet
              key={vertical.key}
              index={index}
              vertical={vertical}
              collected={collected}
              onOpen={() => onVerticalOpen(vertical.id)}
              onCardOpen={onCardOpen}
            />
          );
        })}
      </section>
    </main>
  );
}

function Wallet({
  vertical,
  collected,
  index,
  onOpen,
  onCardOpen,
}: {
  vertical: (typeof verticals)[number];
  collected: BocCard[];
  index: number;
  onOpen: () => void;
  onCardOpen: (id: string) => void;
}) {
  const visibleCards = collected.slice(0, 5);
  const hiddenCount = Math.max(0, collected.length - visibleCards.length);
  return (
    <article
      className="wallet"
      style={{ '--accent': vertical.accent, '--stagger': `${index * 90}ms` } as StyleVars}
      onClick={onOpen}
    >
      <div className="wallet-header">
        <span><i className={vertical.icon} /> {vertical.name}</span>
        <strong>{collected.length} cards</strong>
      </div>
      <div className="peek-stack">
        {visibleCards.map((card, cardIndex) => (
          <button
            type="button"
            key={card.id}
            className="peek-card"
            aria-label={`Open ${card.title} card`}
            onClick={(event) => {
              event.stopPropagation();
              onCardOpen(card.id);
            }}
            style={
              {
                '--accent': vertical.accent,
                '--slot': String(cardIndex),
                '--z': String(cardIndex + 1),
                '--delay': `${cardIndex * 42}ms`,
              } as StyleVars
            }
          >
            <i className={vertical.icon} />
            <span>
              <small>Card</small>
              <b>{card.title}</b>
            </span>
            <strong>{card.factTag}</strong>
          </button>
        ))}
      </div>
      <div className="wallet-pocket">
        <div className="wallet-logo" aria-hidden="true">
          <span>B</span>
          <strong>boccodex</strong>
        </div>
        <span className="wallet-balance">
          <strong>{collected.length} cards collected</strong>
          <em>{hiddenCount > 0 ? `${hiddenCount} more tucked inside` : 'Ask to discover more'}</em>
        </span>
        <button
          className="wallet-open"
          type="button"
          onClick={(event) => {
            event.stopPropagation();
            onOpen();
          }}
          aria-label={`Open ${vertical.name} deck`}
        >
          <span>Open deck</span>
          <i className="ph-bold ph-arrow-right" />
        </button>
      </div>
    </article>
  );
}

function CollectionPage({
  verticalId,
  cards,
  collectedSet,
  rarityFilter,
  sortMode,
  onBack,
  onCardOpen,
  onFilterChange,
  onSortChange,
}: {
  verticalId: number;
  cards: BocCard[];
  collectedSet: Set<string>;
  rarityFilter: Rarity | 'all';
  sortMode: 'newest' | 'oldest' | 'rarity';
  onBack: () => void;
  onCardOpen: (id: string) => void;
  onFilterChange: (rarity: Rarity | 'all') => void;
  onSortChange: (sort: 'newest' | 'oldest' | 'rarity') => void;
}) {
  const vertical = verticals.find((item) => item.id === verticalId) ?? verticals[0];
  const rarityOrder: Record<Rarity, number> = {
    common: 1,
    uncommon: 2,
    rare: 3,
    'ultra-rare': 4,
  };
  const filteredCards = cards
    .filter((card) => card.vertical === vertical.key)
    .filter((card) => rarityFilter === 'all' || card.rarity === rarityFilter)
    .sort((a, b) => {
      if (sortMode === 'rarity') return rarityOrder[b.rarity] - rarityOrder[a.rarity];
      const aTime = Date.parse(a.unlockedAt ?? '2026-05-02T00:00:00.000Z');
      const bTime = Date.parse(b.unlockedAt ?? '2026-05-02T00:00:00.000Z');
      return sortMode === 'newest' ? bTime - aTime : aTime - bTime;
    });

  return (
    <main className="collection-page" style={{ '--accent': vertical.accent } as StyleVars}>
      <section className="collection-hero">
        <button className="ghost-button" type="button" onClick={onBack}>
          <i className="ph-bold ph-arrow-left" /> Home
        </button>
        <div>
          <p className="eyebrow">Vertical {String(vertical.id).padStart(2, '0')}</p>
          <h1>{vertical.name}</h1>
          <p>{vertical.description}</p>
        </div>
      </section>

      <section className="collection-toolbar" aria-label="Collection controls">
        <div className="segmented">
          {(['all', 'common', 'uncommon', 'rare', 'ultra-rare'] as const).map((rarity) => (
            <button
              type="button"
              key={rarity}
              className={rarityFilter === rarity ? 'active' : undefined}
              onClick={() => onFilterChange(rarity)}
            >
              {formatRarity(rarity)}
            </button>
          ))}
        </div>
        <label className="select-label">
          <span>Sort</span>
          <select
            value={sortMode}
            onChange={(event) => onSortChange(event.target.value as typeof sortMode)}
          >
            <option value="newest">Newest</option>
            <option value="oldest">Oldest</option>
            <option value="rarity">Rarity</option>
          </select>
        </label>
      </section>

      <section className="card-grid" aria-label={`${vertical.name} cards`}>
        {filteredCards.map((card) => (
          <button
            className="card-button"
            type="button"
            key={card.id}
            onClick={() => collectedSet.has(card.id) && onCardOpen(card.id)}
          >
            <BocCardView
              card={card}
              discovered={collectedSet.has(card.id)}
              compact
            />
          </button>
        ))}
      </section>
    </main>
  );
}

function ChatPanel({
  open,
  history,
  onAsk,
  onCardOpen,
  onClear,
  onClose,
}: {
  open: boolean;
  history: ChatMessage[];
  onAsk: (question: string) => Promise<void>;
  onCardOpen: (id: string) => void;
  onClear: () => void;
  onClose: () => void;
}) {
  const [question, setQuestion] = useState('');
  const [loading, setLoading] = useState(false);
  const [showPrompts, setShowPrompts] = useState(false);
  const chatEndRef = useRef<HTMLDivElement | null>(null);
  const suggestions = [
    'How do I get a Codice Fiscale?',
    'What exchange options can I explore?',
    'How does JobGate help with internships?',
    'Where can I find wellbeing support?',
  ];

  const submit = async (event?: FormEvent) => {
    event?.preventDefault();
    if (!question.trim() || loading) return;
    setLoading(true);
    const sent = question;
    setQuestion('');
    setShowPrompts(false);
    await onAsk(sent);
    setLoading(false);
  };

  useEffect(() => {
    if (!open) return;
    chatEndRef.current?.scrollIntoView({ block: 'end', behavior: 'smooth' });
  }, [history, open]);

  const showPromptTray = history.length === 0 || showPrompts;

  return (
    <div className={open ? 'chat-layer open' : 'chat-layer'} aria-hidden={!open}>
      <div className="chat-panel" role="dialog" aria-modal="true" aria-label="boccodex AI buddy">
        <header>
          <span className="chat-badge"><i className="ph-duotone ph-sparkle" /> boccodex</span>
          <div className="chat-actions">
            <button
              type="button"
              aria-label="Show prompt ideas"
              aria-pressed={showPrompts}
              onClick={() => setShowPrompts((value) => !value)}
            >
              <i className="ph-bold ph-lightbulb" />
            </button>
            <button
              type="button"
              aria-label="Clear chat history"
              disabled={history.length === 0}
              onClick={onClear}
            >
              <i className="ph-bold ph-trash" />
            </button>
            <button type="button" aria-label="Close chat" onClick={onClose}>
              <i className="ph-bold ph-x" />
            </button>
          </div>
        </header>
        <div className="chat-copy">
          <h2>Ask the AI buddy</h2>
          <p>Useful answers can unlock cards for your Bocconi collection.</p>
        </div>
        {showPromptTray && <div className="suggestions" aria-label="Suggested questions">
          {suggestions.map((item) => (
            <button
              type="button"
              key={item}
              onClick={() => {
                setQuestion(item);
                setShowPrompts(false);
              }}
            >
              {item}
            </button>
          ))}
        </div>}
        <div className="chat-history">
          {history.length === 0 ? (
            <div className="empty-chat">
              <i className="ph-duotone ph-cards" />
              <p>Pick a prompt or ask your own question. The answer will stay in this space, with sources underneath.</p>
            </div>
          ) : (
            history.slice(-8).map((message) => (
              <article className={`message ${message.role}`} key={message.id}>
                <p>{message.text}</p>
                {message.sources?.length ? (
                  <span>{message.sources.slice(0, 2).map(shortSource).join(' · ')}</span>
                ) : null}
                {message.card ? (
                  <ChatCardPreview card={message.card} onOpen={() => onCardOpen(message.card!.id)} />
                ) : null}
              </article>
            ))
          )}
          <div ref={chatEndRef} />
        </div>
        <form className="ask-form" onSubmit={submit}>
          <input
            value={question}
            onChange={(event) => setQuestion(event.target.value)}
            placeholder="Ask about Milan, campus, exchange, careers..."
            aria-label="Question for boccodex"
          />
          <button type="submit" disabled={loading || !question.trim()} aria-label="Send question">
            <i className={loading ? 'ph-bold ph-spinner-gap spin' : 'ph-bold ph-paper-plane-tilt'} />
          </button>
        </form>
      </div>
    </div>
  );
}

function ChatCardPreview({ card, onOpen }: { card: BocCard; onOpen: () => void }) {
  const vertical = getVerticalByKey(card.vertical);
  const fallbackImage = cardImageFallbacks[card.vertical];
  const previewImage = card.imageUrl
    ? { url: card.imageUrl, alt: card.imageAlt ?? card.title }
    : fallbackImage;
  return (
    <button
      className="chat-card-preview"
      type="button"
      onClick={onOpen}
      style={{ '--accent': vertical.accent } as StyleVars}
    >
      <span className="chat-card-art">
        <img src={previewImage.url} alt={previewImage.alt} loading="lazy" />
      </span>
      <span className="chat-card-copy">
        <small>{vertical.name} · {formatRarity(card.rarity)}</small>
        <strong>{card.title}</strong>
        <em>{card.factTag}</em>
      </span>
      <i className="ph-bold ph-arrow-square-out" />
    </button>
  );
}

function BocCardView({
  card,
  discovered,
  compact = false,
}: {
  card: BocCard;
  discovered: boolean;
  compact?: boolean;
}) {
  const [flipped, setFlipped] = useState(false);
  const [failedImageUrl, setFailedImageUrl] = useState<string | null>(null);
  const vertical = getVerticalByKey(card.vertical);
  const fallbackImage = cardImageFallbacks[card.vertical];
  const displayImage = card.imageUrl
    ? { url: card.imageUrl, alt: card.imageAlt ?? card.title }
    : fallbackImage;
  const cardImageUrl = displayImage.url && failedImageUrl !== displayImage.url ? displayImage.url : null;

  const onPointerMove = (event: PointerEvent<HTMLElement>) => {
    const target = event.currentTarget;
    const rect = target.getBoundingClientRect();
    const x = ((event.clientX - rect.left) / rect.width - 0.5) * 18;
    const y = ((event.clientY - rect.top) / rect.height - 0.5) * -18;
    target.style.setProperty('--tilt-x', `${y}deg`);
    target.style.setProperty('--tilt-y', `${x}deg`);
  };

  const resetTilt = (event: PointerEvent<HTMLElement>) => {
    event.currentTarget.style.setProperty('--tilt-x', '0deg');
    event.currentTarget.style.setProperty('--tilt-y', '0deg');
  };

  return (
    <article
      className={`boc-card ${compact ? 'compact' : ''} ${flipped ? 'flipped' : ''}`}
      role="article"
      aria-label={`${card.title} card, ${formatRarity(card.rarity)} rarity`}
      style={
        {
          '--accent': vertical.accent,
          '--tilt-x': '0deg',
          '--tilt-y': '0deg',
        } as StyleVars
      }
      onPointerMove={onPointerMove}
      onPointerLeave={resetTilt}
      onClick={(event) => {
        if (compact) return;
        event.stopPropagation();
        if (discovered) setFlipped((value) => !value);
      }}
    >
      <div className="card-inner">
        <div className={discovered ? 'card-face' : 'card-face locked'}>
          {discovered ? (
            <>
              <div className="card-strip">
                <span>{vertical.name}</span>
                <i className={vertical.icon} />
              </div>
              <div className={cardImageUrl ? 'card-art with-image' : 'card-art'}>
                {cardImageUrl ? (
                  <img
                    src={cardImageUrl}
                    alt={displayImage.alt}
                    loading="lazy"
                    onError={() => setFailedImageUrl(cardImageUrl)}
                  />
                ) : (
                  <i className={vertical.icon} />
                )}
                <span>{card.factTag}</span>
              </div>
              <div className="card-body">
                <h3>{card.title}</h3>
                <p>{card.body}</p>
              </div>
              <footer>
                <span className={`rarity ${card.rarity}`}>{formatRarity(card.rarity)}</span>
                <span>{card.id}</span>
                <i className="ph-bold ph-share-network" />
              </footer>
            </>
          ) : (
            <CardBack locked />
          )}
        </div>
        <div className="card-back">
          <CardBack />
        </div>
      </div>
    </article>
  );
}

function CardBack({ locked = false }: { locked?: boolean }) {
  return (
    <div className={locked ? 'back-design locked-back' : 'back-design'}>
      <span className="back-logo">B</span>
      <strong>BocCard</strong>
      {locked && <i className="ph-bold ph-question" />}
    </div>
  );
}

function CardDetailModal({
  card,
  related,
  onClose,
  onShareRoute,
}: {
  card: BocCard;
  related: BocCard[];
  onClose: () => void;
  onShareRoute: () => void;
}) {
  const vertical = getVerticalByKey(card.vertical);
  return (
    <div className="modal-layer" role="presentation">
      <div className="card-modal" role="dialog" aria-modal="true" aria-label={card.title}>
        <button className="modal-close" type="button" aria-label="Close card" onClick={onClose}>
          <i className="ph-bold ph-x" />
        </button>
        <BocCardView card={card} discovered />
        <section className="modal-copy">
          <p className="eyebrow">{formatRarity(card.rarity)} · {card.id}</p>
          <h2>{card.title}</h2>
          <div className="card-meta-row">
            <span><i className={vertical.icon} /> {vertical.name}</span>
            <span>{card.factTag}</span>
            <span>{card.datasetSnapshot}</span>
          </div>
          <p>{card.longBody ?? card.body}</p>
          <span className="source-chip">{card.sourceLabel}</span>
          <div className="share-row" aria-label="Share card">
            <button type="button" aria-label="Download Instagram share image" onClick={() => downloadShareImage(card)}>
              <i className="ph-bold ph-instagram-logo" />
            </button>
            <button type="button" aria-label="Share on X" onClick={() => openShare('x', card)}>
              <i className="ph-bold ph-x-logo" />
            </button>
            <button type="button" aria-label="Share on LinkedIn" onClick={() => openShare('linkedin', card)}>
              <i className="ph-bold ph-linkedin-logo" />
            </button>
            <button type="button" aria-label="Share on WhatsApp" onClick={() => openShare('whatsapp', card)}>
              <i className="ph-bold ph-whatsapp-logo" />
            </button>
            <button type="button" aria-label="Open share page" onClick={onShareRoute}>
              <i className="ph-bold ph-link" />
            </button>
          </div>
          <div className="related-strip" aria-label="Related cards">
            {related.slice(0, 6).map((item) => (
              <span key={item.id}>{item.title}</span>
            ))}
          </div>
        </section>
      </div>
    </div>
  );
}

function SharePage({ card, onHome }: { card: BocCard | null; onHome: () => void }) {
  if (!card) {
    return (
      <main className="share-page">
        <h1>Card not found</h1>
        <button className="primary-button" type="button" onClick={onHome}>Back to boccodex</button>
      </main>
    );
  }

  return (
    <main className="share-page">
      <BocCardView card={card} discovered />
      <section>
        <p className="eyebrow">Shared from boccodex</p>
        <h1>{card.title}</h1>
        <p>{card.body}</p>
        <button className="primary-button" type="button" onClick={() => downloadShareImage(card)}>
          <i className="ph-bold ph-download-simple" /> Download share image
        </button>
      </section>
    </main>
  );
}

function isAskResponse(payload: unknown): payload is AskResponse {
  if (!payload || typeof payload !== 'object') return false;
  const candidate = payload as Partial<AskResponse>;
  return (
    typeof candidate.answer === 'string' &&
    Array.isArray(candidate.sources) &&
    typeof candidate.verticale === 'string' &&
    verticals.some((vertical) => vertical.key === candidate.verticale)
  );
}

function isCardDecisionResponse(payload: unknown): payload is CardDecisionResponse {
  if (!payload || typeof payload !== 'object') return false;
  const candidate = payload as Partial<CardDecisionResponse>;
  return (
    (candidate.action === 'created' ||
      candidate.action === 'already_owned' ||
      candidate.action === 'skipped') &&
    (candidate.card === null || candidate.card === undefined || isBocCard(candidate.card)) &&
    (candidate.existingCardId === null ||
      candidate.existingCardId === undefined ||
      typeof candidate.existingCardId === 'string') &&
    typeof candidate.message === 'string'
  );
}

function isBocCard(payload: unknown): payload is BocCard {
  if (!payload || typeof payload !== 'object') return false;
  const candidate = payload as Partial<BocCard>;
  return (
    typeof candidate.id === 'string' &&
    typeof candidate.title === 'string' &&
    typeof candidate.body === 'string' &&
    typeof candidate.factTag === 'string' &&
    typeof candidate.imageQuery === 'string' &&
    (candidate.imageUrl === null ||
      candidate.imageUrl === undefined ||
      typeof candidate.imageUrl === 'string') &&
    (candidate.imageAlt === null ||
      candidate.imageAlt === undefined ||
      typeof candidate.imageAlt === 'string') &&
    (candidate.imageSource === null ||
      candidate.imageSource === undefined ||
      ['dataset', 'unsplash', 'placeholder'].includes(candidate.imageSource)) &&
    typeof candidate.isStarter === 'boolean' &&
    typeof candidate.sourceLabel === 'string' &&
    Array.isArray(candidate.tags) &&
    verticals.some((vertical) => vertical.key === candidate.vertical) &&
    ['common', 'uncommon', 'rare', 'ultra-rare'].includes(candidate.rarity ?? '') &&
    ['2026-05-02', 'live'].includes(candidate.datasetSnapshot ?? '')
  );
}

function createFallbackCard(question: string, answer: AskResponse, cards: BocCard[]): BocCard {
  const vertical = getVerticalByKey(answer.verticale);
  const sameVerticalCards = cards.filter((card) => card.vertical === answer.verticale);
  const imageSource = sameVerticalCards.find((card) => card.imageUrl);
  const timestamp = Date.now().toString(36).toUpperCase();
  const title = makeCardTitle(question);

  return {
    id: `V${String(vertical.id).padStart(2, '0')}-${timestamp}`,
    vertical: answer.verticale,
    title,
    body: trimCardText(answer.answer, 240),
    longBody: answer.answer,
    factTag: extractFactTag(answer.answer),
    imageQuery: question,
    imageUrl: imageSource?.imageUrl,
    imageAlt: imageSource?.imageAlt ?? title,
    imageSource: imageSource?.imageSource ?? 'placeholder',
    rarity: 'uncommon',
    isStarter: false,
    sourceLabel: answer.sources[0] ? shortSource(answer.sources[0]) : 'Bocconi 2026 dataset',
    datasetSnapshot: answer.sources.some((source) => /^https?:\/\//.test(source))
      ? 'live'
      : '2026-05-02',
    unlockedAt: new Date().toISOString(),
    tags: [...new Set(question.toLowerCase().match(/[a-z0-9]{4,}/g) ?? [])].slice(0, 6),
  };
}

function makeCardTitle(question: string) {
  const stopwords = new Set([
    'what',
    'which',
    'where',
    'when',
    'about',
    'bocconi',
    'student',
    'students',
    'campus',
    'please',
  ]);
  const words = question
    .replace(/[^\w\s-]/g, ' ')
    .split(/\s+/)
    .filter((word) => word.length > 2 && !stopwords.has(word.toLowerCase()))
    .slice(0, 6);
  return (words.length ? words : ['Campus', 'Discovery'])
    .join(' ')
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function trimCardText(text: string, maxLength: number) {
  const compact = text.replace(/\s+/g, ' ').trim();
  if (compact.length <= maxLength) return compact;
  const clipped = compact.slice(0, maxLength - 1);
  return `${clipped.slice(0, clipped.lastIndexOf(' '))}...`;
}

function extractFactTag(answer: string) {
  const match = answer.match(/(~?€\s?[\d,.]+|[\d,.]+\s?(?:%|ECTS|countries|partners|months|weeks|hours|days))/i);
  return match?.[0] ?? '2026 snapshot';
}

async function readJsonResponse(
  response: Response,
  emptyMessage: string,
  invalidMessage: string,
): Promise<unknown> {
  const text = await response.text();
  if (!text.trim()) {
    throw new Error(emptyMessage);
  }

  try {
    return JSON.parse(text);
  } catch {
    throw new Error(invalidMessage);
  }
}

function getResponseDetail(payload: unknown) {
  if (!payload || typeof payload !== 'object') return null;
  const candidate = payload as { detail?: unknown; message?: unknown };
  if (typeof candidate.detail === 'string') return candidate.detail;
  if (typeof candidate.message === 'string') return candidate.message;
  return null;
}

function shortSource(source: string) {
  const clean = source.split('/').pop()?.replace(/\.md$/i, '') ?? source;
  return clean.replace(/[-_]+/g, ' ').slice(0, 42);
}

function formatRarity(rarity: Rarity | 'all') {
  if (rarity === 'all') return 'All';
  return rarity
    .split('-')
    .map((part) => part[0].toUpperCase() + part.slice(1))
    .join(' ');
}

function openShare(platform: 'x' | 'linkedin' | 'whatsapp', card: BocCard) {
  const shareUrl = `${window.location.origin}/share/${encodeURIComponent(card.id)}`;
  const text = encodeURIComponent(`${card.title} - discovered on boccodex`);
  const url = encodeURIComponent(shareUrl);
  const targets = {
    x: `https://twitter.com/intent/tweet?text=${text}&url=${url}`,
    linkedin: `https://www.linkedin.com/sharing/share-offsite/?url=${url}`,
    whatsapp: `https://wa.me/?text=${text}%20${url}`,
  };
  window.open(targets[platform], '_blank', 'noopener,noreferrer');
}

function downloadShareImage(card: BocCard) {
  const canvas = document.createElement('canvas');
  canvas.width = 1080;
  canvas.height = 1512;
  const ctx = canvas.getContext('2d');
  if (!ctx) return;
  const vertical = getVerticalByKey(card.vertical);

  ctx.fillStyle = '#233835';
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  ctx.fillStyle = vertical.accent;
  ctx.globalAlpha = 0.2;
  ctx.beginPath();
  ctx.arc(880, 220, 260, 0, Math.PI * 2);
  ctx.fill();
  ctx.globalAlpha = 1;

  roundRect(ctx, 220, 120, 640, 960, 54, '#F5F0E8');
  ctx.strokeStyle = '#633C2D';
  ctx.lineWidth = 12;
  roundStroke(ctx, 220, 120, 640, 960, 54);
  roundRect(ctx, 250, 150, 580, 84, 22, vertical.accent);

  ctx.fillStyle = '#F5F0E8';
  ctx.font = '700 34px Sora, Inter, sans-serif';
  ctx.fillText(vertical.name, 282, 205);

  roundRect(ctx, 270, 280, 540, 390, 28, vertical.accent);
  ctx.fillStyle = '#F5F0E8';
  ctx.font = '700 84px Sora, Inter, sans-serif';
  ctx.fillText(card.factTag.slice(0, 12), 315, 495);

  ctx.fillStyle = '#1A1A1A';
  ctx.font = '700 52px Sora, Inter, sans-serif';
  wrapText(ctx, card.title, 282, 760, 520, 62);
  ctx.font = '400 34px Inter, sans-serif';
  wrapText(ctx, card.body, 282, 870, 520, 48);

  ctx.fillStyle = '#F5F0E8';
  ctx.font = '700 38px Sora, Inter, sans-serif';
  ctx.fillText('Discovered on BocCard', 270, 1260);
  ctx.font = '400 28px Inter, sans-serif';
  ctx.fillText('bocconi.boccard.app', 270, 1310);

  const link = document.createElement('a');
  link.download = `${card.id}-${card.title.toLowerCase().replace(/\W+/g, '-')}.png`;
  link.href = canvas.toDataURL('image/png');
  link.click();
}

function roundRect(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  width: number,
  height: number,
  radius: number,
  fill: string,
) {
  roundedPath(ctx, x, y, width, height, radius);
  ctx.fillStyle = fill;
  ctx.fill();
}

function roundStroke(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  width: number,
  height: number,
  radius: number,
) {
  roundedPath(ctx, x, y, width, height, radius);
  ctx.stroke();
}

function roundedPath(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  width: number,
  height: number,
  radius: number,
) {
  ctx.beginPath();
  ctx.moveTo(x + radius, y);
  ctx.arcTo(x + width, y, x + width, y + height, radius);
  ctx.arcTo(x + width, y + height, x, y + height, radius);
  ctx.arcTo(x, y + height, x, y, radius);
  ctx.arcTo(x, y, x + width, y, radius);
  ctx.closePath();
}

function wrapText(
  ctx: CanvasRenderingContext2D,
  text: string,
  x: number,
  y: number,
  maxWidth: number,
  lineHeight: number,
) {
  const words = text.split(' ');
  let line = '';
  let currentY = y;
  words.forEach((word) => {
    const test = `${line}${word} `;
    if (ctx.measureText(test).width > maxWidth && line) {
      ctx.fillText(line, x, currentY);
      line = `${word} `;
      currentY += lineHeight;
    } else {
      line = test;
    }
  });
  ctx.fillText(line, x, currentY);
}

type StyleVars = React.CSSProperties & Record<`--${string}`, string>;

export default App;
