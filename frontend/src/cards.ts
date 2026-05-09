export type VerticalKey =
  | 'relocation'
  | 'life_on_campus'
  | 'study_abroad'
  | 'career_readiness';

export type Rarity = 'common' | 'uncommon' | 'rare' | 'ultra-rare';

export interface BocCard {
  id: string;
  vertical: VerticalKey;
  title: string;
  body: string;
  longBody?: string;
  factTag: string;
  imageQuery: string;
  rarity: Rarity;
  isStarter: boolean;
  sourceLabel: string;
  datasetSnapshot: '2026-05-02' | 'live';
  unlockedAt?: string;
  tags: string[];
}

export interface ChatMessage {
  id: string;
  role: 'user' | 'assistant';
  text: string;
  createdAt: string;
  sources?: string[];
  vertical?: VerticalKey;
}

export interface AppState {
  collectedCardIds: string[];
  chatHistory: ChatMessage[];
  firstVisit: boolean;
  discoveredCards: BocCard[];
}

export const APP_STATE_KEY = 'boccodex-state-v1';

export const verticals = [
  {
    id: 1,
    key: 'relocation',
    name: 'Life in Milan',
    navLabel: 'Milan',
    mobileLabel: 'Milan',
    accent: '#B86A4A',
    icon: 'ph-bold ph-city',
    description: 'Housing, transport, health paperwork, neighborhoods, and the basics of settling into Milan.',
  },
  {
    id: 2,
    key: 'life_on_campus',
    name: 'Life on Campus',
    navLabel: 'Campus',
    mobileLabel: 'Campus',
    accent: '#16B88B',
    icon: 'ph-bold ph-student',
    description: 'Dining, libraries, events, associations, sport, inclusion, and support services around Bocconi.',
  },
  {
    id: 3,
    key: 'study_abroad',
    name: 'Study Abroad',
    navLabel: 'Abroad',
    mobileLabel: 'Abroad',
    accent: '#2C786C',
    icon: 'ph-bold ph-airplane-tilt',
    description: 'Exchange programs, double degrees, Erasmus+, summer schools, and credit recognition.',
  },
  {
    id: 4,
    key: 'career_readiness',
    name: 'Career Readiness',
    navLabel: 'Careers',
    mobileLabel: 'Careers',
    accent: '#633C2D',
    icon: 'ph-bold ph-briefcase',
    description: 'CVs, internships, JobGate, recruiting, scholarships, programs, departments, and alumni pathways.',
  },
] as const satisfies readonly {
  id: number;
  key: VerticalKey;
  name: string;
  navLabel: string;
  mobileLabel: string;
  accent: string;
  icon: string;
  description: string;
}[];

export function getVerticalByKey(key: VerticalKey) {
  return verticals.find((vertical) => vertical.key === key) ?? verticals[0];
}

export const CARD_POOL: BocCard[] = [
  {
    id: 'V01-001',
    vertical: 'relocation',
    title: 'Monthly Student Budget',
    body: 'Plan rent, food, transport, books, and small city costs before choosing a room. Milan is manageable when the fixed expenses are visible early.',
    factTag: '~€900/month',
    imageQuery: 'student budget milan',
    rarity: 'common',
    isStarter: true,
    sourceLabel: 'YesMilano cost of living',
    datasetSnapshot: '2026-05-02',
    tags: ['budget', 'rent', 'milan'],
  },
  {
    id: 'V01-002',
    vertical: 'relocation',
    title: 'ATM City Pass',
    body: 'The Milan public transport network connects Bocconi with metro, tram, bus, and rail links. Student passes usually make daily commuting cheaper.',
    factTag: 'ATM network',
    imageQuery: 'milan tram pass',
    rarity: 'common',
    isStarter: true,
    sourceLabel: 'Comune Milano transport data',
    datasetSnapshot: '2026-05-02',
    tags: ['atm', 'transport'],
  },
  {
    id: 'V01-003',
    vertical: 'relocation',
    title: 'Health Registration',
    body: 'International students should check insurance and SSN registration requirements before arrival. Keep identity documents and fiscal code details ready.',
    factTag: 'SSN basics',
    imageQuery: 'healthcare milan student',
    rarity: 'common',
    isStarter: true,
    sourceLabel: 'Bocconi health services',
    datasetSnapshot: '2026-05-02',
    tags: ['health', 'ssn'],
  },
  {
    id: 'V01-004',
    vertical: 'relocation',
    title: 'Permit Basics',
    body: 'Non-EU students usually need a permit of stay after arrival. Start the paperwork quickly because deadlines and appointments matter.',
    factTag: '8 days',
    imageQuery: 'permit stay italy',
    rarity: 'common',
    isStarter: true,
    sourceLabel: 'Bocconi permit guidance',
    datasetSnapshot: '2026-05-02',
    tags: ['permit', 'visa'],
  },
  {
    id: 'V01-005',
    vertical: 'relocation',
    title: 'Neighborhood Rent Map',
    body: 'Rooms close to campus cost more, while outer neighborhoods can trade price for commute time. Compare rent with transport links, not only distance.',
    factTag: 'Zone tradeoff',
    imageQuery: 'milan neighborhoods rent',
    rarity: 'common',
    isStarter: true,
    sourceLabel: 'YesMilano neighborhoods',
    datasetSnapshot: '2026-05-02',
    tags: ['housing', 'neighborhood'],
  },
  {
    id: 'V02-001',
    vertical: 'life_on_campus',
    title: 'Campus Dining',
    body: 'Dining options around campus cover quick lunches, canteen-style meals, and nearby cafes. Check current hours before planning around evening classes.',
    factTag: 'Mensa guide',
    imageQuery: 'campus dining bocconi',
    rarity: 'common',
    isStarter: true,
    sourceLabel: 'Bocconi campus services',
    datasetSnapshot: '2026-05-02',
    tags: ['dining', 'mensa'],
  },
  {
    id: 'V02-002',
    vertical: 'life_on_campus',
    title: 'Sports Access',
    body: 'Bocconi Sport coordinates activities, teams, and facilities for students who want training or competition alongside classes.',
    factTag: 'Bocconi Sport',
    imageQuery: 'bocconi sport',
    rarity: 'common',
    isStarter: true,
    sourceLabel: 'Bocconi sport pages',
    datasetSnapshot: '2026-05-02',
    tags: ['sport', 'facilities'],
  },
  {
    id: 'V02-003',
    vertical: 'life_on_campus',
    title: 'Association Map',
    body: 'Student associations are one of the fastest ways to find peers around interests, sectors, causes, and cultural communities.',
    factTag: 'Clubs',
    imageQuery: 'student association bocconi',
    rarity: 'common',
    isStarter: true,
    sourceLabel: 'Bocconi student associations',
    datasetSnapshot: '2026-05-02',
    tags: ['clubs', 'association'],
  },
  {
    id: 'V02-004',
    vertical: 'life_on_campus',
    title: 'Library Access',
    body: 'The library is a study base and a research gateway. Opening hours can change by season, exam period, and service area.',
    factTag: 'Library',
    imageQuery: 'university library study',
    rarity: 'common',
    isStarter: true,
    sourceLabel: 'Bocconi library',
    datasetSnapshot: '2026-05-02',
    tags: ['library', 'study'],
  },
  {
    id: 'V02-005',
    vertical: 'life_on_campus',
    title: 'Wellbeing Support',
    body: 'Students can look for psychological support, inclusion services, and wellbeing resources through Bocconi support channels.',
    factTag: 'Support',
    imageQuery: 'student wellbeing support',
    rarity: 'common',
    isStarter: true,
    sourceLabel: 'Bocconi wellbeing',
    datasetSnapshot: '2026-05-02',
    tags: ['wellbeing', 'support'],
  },
  {
    id: 'V03-001',
    vertical: 'study_abroad',
    title: 'Partner Network',
    body: 'Bocconi mobility relies on a broad international partner network. Destination fit depends on program, year, language, and selection rules.',
    factTag: 'Global partners',
    imageQuery: 'exchange partner university',
    rarity: 'common',
    isStarter: true,
    sourceLabel: 'Bocconi partner schools',
    datasetSnapshot: '2026-05-02',
    tags: ['partners', 'exchange'],
  },
  {
    id: 'V03-002',
    vertical: 'study_abroad',
    title: 'Erasmus Timeline',
    body: 'Erasmus+ opportunities follow application windows and eligibility checks. Mark the deadlines early so transcript and language requirements do not surprise you.',
    factTag: 'Apply early',
    imageQuery: 'erasmus timeline',
    rarity: 'common',
    isStarter: true,
    sourceLabel: 'Bocconi exchange timeline',
    datasetSnapshot: '2026-05-02',
    tags: ['erasmus', 'timeline'],
  },
  {
    id: 'V03-003',
    vertical: 'study_abroad',
    title: 'Double Degree Paths',
    body: 'Double degrees combine Bocconi study with a partner institution. They are more selective and structured than a standard exchange.',
    factTag: 'DD network',
    imageQuery: 'double degree university',
    rarity: 'common',
    isStarter: true,
    sourceLabel: 'Bocconi double degree',
    datasetSnapshot: '2026-05-02',
    tags: ['double-degree'],
  },
  {
    id: 'V03-004',
    vertical: 'study_abroad',
    title: 'Summer Schools',
    body: 'Summer schools offer shorter international or Bocconi-based study experiences. They can be useful when a semester abroad is not the right fit.',
    factTag: 'Summer',
    imageQuery: 'summer school university',
    rarity: 'common',
    isStarter: true,
    sourceLabel: 'Bocconi summer school',
    datasetSnapshot: '2026-05-02',
    tags: ['summer', 'mobility'],
  },
  {
    id: 'V03-005',
    vertical: 'study_abroad',
    title: 'Credit Recognition',
    body: 'Exchange courses need academic recognition, so students should check rules before choosing exams abroad. Approval protects your study plan.',
    factTag: 'ECTS check',
    imageQuery: 'university credits abroad',
    rarity: 'common',
    isStarter: true,
    sourceLabel: 'Bocconi recognition rules',
    datasetSnapshot: '2026-05-02',
    tags: ['credits', 'recognition'],
  },
  {
    id: 'V04-001',
    vertical: 'career_readiness',
    title: 'MSc Catalog',
    body: 'Bocconi MSc programs connect management, economics, finance, data, policy, and legal pathways. Compare career outcomes with course structure.',
    factTag: 'MSc paths',
    imageQuery: 'business school masters',
    rarity: 'common',
    isStarter: true,
    sourceLabel: 'Bocconi MSc programs',
    datasetSnapshot: '2026-05-02',
    tags: ['msc', 'programs'],
  },
  {
    id: 'V04-002',
    vertical: 'career_readiness',
    title: 'JobGate Guide',
    body: 'JobGate is the practical bridge to internships and job postings. Keep your profile clean, complete, and aligned with your target roles.',
    factTag: 'JobGate',
    imageQuery: 'career portal internship',
    rarity: 'common',
    isStarter: true,
    sourceLabel: 'Bocconi JobGate',
    datasetSnapshot: '2026-05-02',
    tags: ['jobgate', 'internship'],
  },
  {
    id: 'V04-003',
    vertical: 'career_readiness',
    title: 'Scholarship Types',
    body: 'Funding options can include merit, need-based, and international support. Rules depend on academic year and student profile.',
    factTag: 'Aid options',
    imageQuery: 'university scholarship',
    rarity: 'common',
    isStarter: true,
    sourceLabel: 'Bocconi funding',
    datasetSnapshot: '2026-05-02',
    tags: ['scholarship', 'funding'],
  },
  {
    id: 'V04-004',
    vertical: 'career_readiness',
    title: 'Department Map',
    body: 'Departments shape research, faculty expertise, and academic identity. They help you understand where programs and disciplines sit inside Bocconi.',
    factTag: 'Faculty map',
    imageQuery: 'university departments',
    rarity: 'common',
    isStarter: true,
    sourceLabel: 'Bocconi departments',
    datasetSnapshot: '2026-05-02',
    tags: ['departments', 'faculty'],
  },
  {
    id: 'V04-005',
    vertical: 'career_readiness',
    title: 'Alumni Network',
    body: 'The alumni network can support mentoring, sector exploration, and long-term professional connections beyond the first job search.',
    factTag: 'Alumni',
    imageQuery: 'alumni mentoring business school',
    rarity: 'common',
    isStarter: true,
    sourceLabel: 'Bocconi alumni',
    datasetSnapshot: '2026-05-02',
    tags: ['alumni', 'mentoring'],
  },
];

export const starterCardIds = CARD_POOL.filter((card) => card.isStarter).map((card) => card.id);
