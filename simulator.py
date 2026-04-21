"""
============================================================================
Apple M2 Pro SoC — Hardware-Software Co-Simulation  simulator.py  v2.0
============================================================================
Gereksinimler:
    pip install pygame
    m2pro_engine.so  (aynı klasörde olmalı)

Klavye:
    1-0   → Senaryo 1-10
    F1-F10 → Senaryo 11-20
    R     → Sıfırla
    N     → OS Background Noise aç/kapat
    G     → GC (Garbage Collection) tetikle
    +/-   → CPU sıcaklığı ±5°C
    [/]   → GPU sıcaklığı ±5°C
    Sol klik   → Başlangıç düğümü seç
    Sağ klik   → Bitiş düğümü seç + rota hesapla
    Orta klik  → Düğüme INTERACTIVE görev ata
============================================================================
"""

import sys, math, time, random
from dataclasses import dataclass
import pygame

try:
    import m2pro_engine
except ImportError:
    print("[HATA] m2pro_engine.so bulunamadı — önce derleyin.")
    sys.exit(1)


# ============================================================================
# EKRAN VE ZAMANLAMA SABİTLERİ
# ============================================================================
SCREEN_W, SCREEN_H   = 1500, 840
FPS                  = 60
TICK_INTERVAL_MS     = 400    # Simülasyon tick aralığı (ms)
NOISE_INTERVAL_MS    = 1000   # OS arka plan gürültüsü aralığı (ms)
GC_INTERVAL_MS       = 8000   # Otomatik GC aralığı (ms)

# ============================================================================
# RENK PALETİ  —  Apple Dark Mode temeli
# ============================================================================
BG              = (12,  12,  16)
PANEL_BG        = (22,  22,  30)
PANEL_BORDER    = (45,  45,  60)
PANEL_HEADER    = (32,  32,  44)

# Düğüm renkleri
C_PCORE         = (64,  130, 240)   # Mavi      — P-Core
C_ECORE         = (50,  190, 130)   # Yeşil     — E-Core
C_GPU           = (200, 80,  200)   # Mor       — GPU Core
C_NE            = (240, 160, 40)    # Turuncu   — Neural Engine
C_ALU           = (140, 90,  220)   # Leylak    — ALU
C_REGFILE       = (150, 110, 195)   # Açık leylak
C_L1            = (70,  165, 200)   # Açık mavi — L1 Cache
C_L2            = (55,  140, 185)   # Orta mavi — L2 Cache
C_SLC           = (210, 165, 50)    # Altın     — SLC
C_RAM           = (220, 95,  60)    # Kırmızı-turuncu — RAM
C_IO            = (120, 120, 145)   # Gri       — IO Hub
C_SSD           = (90,  90,  115)   # Koyu gri  — NVMe SSD

C_BUSY          = (210, 55,  55)    # Kırmızı    — Meşgul
C_DIRTY         = (255, 140, 0)     # Turuncu    — Dirty cache
C_THROTTLE      = (230, 110, 30)    # Amber      — Throttled
C_SEL_START     = (255, 230, 50)    # Sarı       — Seçili başlangıç
C_SEL_END       = (50,  235, 120)   # Yeşil      — Seçili bitiş

# Kenar renkleri
E_DEFAULT       = (45,  45,  58)    # Koyu gri
E_ACTIVE        = (255, 215, 50)    # Altın sarı
E_PACKET        = (255, 255, 255)   # Beyaz

# Metin
T_PRIMARY       = (220, 220, 232)
T_SECONDARY     = (130, 130, 155)
T_ACCENT        = (255, 210, 50)
T_ALERT         = (220, 70,  70)
T_SUCCESS       = (70,  200, 110)
T_INFO          = (80,  160, 230)

# Düğüm yarıçapları
R_SMALL  = 7    # ALU, RF
R_MED    = 9    # L1, L2, GPU, NE
R_LARGE  = 12   # P-Core, E-Core
R_HUB    = 15   # SLC, RAM, IO, SSD


# ============================================================================
# YARDIMCI FONKSİYONLAR
# ============================================================================

def node_style(ntype, is_busy, is_dirty, throttling, gpu_throttling):
    """Düğüm tipi + durum → (renk, yarıçap)"""
    if is_busy:
        return C_BUSY, R_MED
    if is_dirty:
        return C_DIRTY, R_MED

    tmap = {
        m2pro_engine.NodeType.P_CORE:        (C_PCORE,   R_LARGE),
        m2pro_engine.NodeType.E_CORE:        (C_ECORE,   R_LARGE),
        m2pro_engine.NodeType.ALU:           (C_ALU,     R_SMALL),
        m2pro_engine.NodeType.REGISTER_FILE: (C_REGFILE, R_SMALL),
        m2pro_engine.NodeType.L1_CACHE:      (C_L1,      R_MED),
        m2pro_engine.NodeType.L2_CACHE:      (C_L2,      R_MED),
        m2pro_engine.NodeType.SLC:           (C_SLC,     R_HUB + 2),
        m2pro_engine.NodeType.UNIFIED_RAM:   (C_RAM,     R_HUB + 4),
        m2pro_engine.NodeType.IO_HUB:        (C_IO,      R_HUB),
        m2pro_engine.NodeType.NVME_SSD:      (C_SSD,     R_HUB),
        m2pro_engine.NodeType.GPU_CORE:      (C_GPU,     R_MED),
        m2pro_engine.NodeType.NEURAL_ENGINE: (C_NE,      R_MED),
    }
    color, radius = tmap.get(ntype, (C_L1, R_MED))

    if throttling and ntype == m2pro_engine.NodeType.P_CORE:
        color = C_THROTTLE
    if gpu_throttling and ntype == m2pro_engine.NodeType.GPU_CORE:
        color = C_THROTTLE

    return color, radius


def lerp_color(c1, c2, t):
    t = max(0.0, min(1.0, t))
    return (int(c1[0] + (c2[0]-c1[0])*t),
            int(c1[1] + (c2[1]-c1[1])*t),
            int(c1[2] + (c2[2]-c1[2])*t))


def edge_color_width(traffic):
    """Trafik [0,1] → (renk, kalınlık). Isı haritası: gri→sarı→kırmızı"""
    if traffic < 0.5:
        col = lerp_color(E_DEFAULT, (200, 200, 60), traffic * 2)
        w   = 1
    else:
        col = lerp_color((200, 200, 60), (220, 50, 50), (traffic - 0.5) * 2)
        w   = max(1, int(traffic * 4))
    return col, w


def lerp_pos(a, b, t):
    return (int(a[0] + (b[0]-a[0])*t), int(a[1] + (b[1]-a[1])*t))


def draw_capacity_bar(surf, cx, cy, radius, load_ratio, color):
    """Düğümün üzerinde küçük doluluk çubuğu çizer."""
    bw = radius * 2 + 4
    bh = 4
    bx = cx - bw // 2
    by = cy - radius - 8
    # Arka plan
    pygame.draw.rect(surf, (40, 40, 55), (bx, by, bw, bh), border_radius=2)
    # Doluluk
    fill_w = int(bw * min(1.0, load_ratio))
    if fill_w > 0:
        fill_color = lerp_color((60, 200, 100), (220, 50, 50), load_ratio)
        pygame.draw.rect(surf, fill_color, (bx, by, fill_w, bh), border_radius=2)


def find_node_by_name(sim, partial):
    for nid in sim.get_all_node_ids():
        if partial in sim.get_node_name(nid):
            return nid
    raise ValueError(f"Düğüm bulunamadı: {partial}")


def find_all_nodes_by_type(sim, ntype):
    return [nid for nid in sim.get_all_node_ids()
            if sim.get_node_type(nid) == ntype]


@dataclass
class TaskProfile:
    name: str
    qos: object
    cpu: float
    gpu: float
    ram: float
    ssd: float
    ne: float
    duration: int
    thermal: float
    preferred_unit: str
    notes: str


TASK_LIBRARY = [
    TaskProfile("Open Browser Tab", m2pro_engine.TaskPriority.INTERACTIVE,
                0.25, 0.10, 0.20, 0.05, 0.0, 80, 0.10, "P-Core",
                "Yeni sekme açma, düşük latency"),
    TaskProfile("Scroll Web Page", m2pro_engine.TaskPriority.INTERACTIVE,
                0.20, 0.35, 0.15, 0.0, 0.0, 60, 0.10, "GPU + P-Core",
                "UI rendering + compositing"),
    TaskProfile("Open Finder Folder", m2pro_engine.TaskPriority.INTERACTIVE,
                0.20, 0.10, 0.15, 0.10, 0.0, 90, 0.08, "P-Core",
                "Dosya listesi + thumbnail"),
    TaskProfile("IDE Autocomplete", m2pro_engine.TaskPriority.INTERACTIVE,
                0.35, 0.0, 0.20, 0.0, 0.0, 50, 0.10, "P-Core",
                "Düşük gecikme şart"),
    TaskProfile("Video Call Mute Toggle", m2pro_engine.TaskPriority.INTERACTIVE,
                0.15, 0.05, 0.10, 0.0, 0.0, 25, 0.05, "P-Core",
                "Anlık kullanıcı etkileşimi"),
    TaskProfile("Xcode Build Project", m2pro_engine.TaskPriority.USER_INITIATED,
                0.95, 0.0, 0.65, 0.30, 0.0, 500, 0.85, "P-Core Cluster",
                "Parallel compile"),
    TaskProfile("VSCode Compile C++", m2pro_engine.TaskPriority.USER_INITIATED,
                0.90, 0.0, 0.55, 0.20, 0.0, 420, 0.75, "P-Core",
                "Compiler yoğun yük"),
    TaskProfile("ZIP Extract 10GB", m2pro_engine.TaskPriority.USER_INITIATED,
                0.65, 0.0, 0.45, 0.70, 0.0, 350, 0.45, "P-Core + SSD",
                "CPU + IO karışık"),
    TaskProfile("Export PDF", m2pro_engine.TaskPriority.USER_INITIATED,
                0.55, 0.15, 0.35, 0.20, 0.0, 260, 0.35, "P-Core",
                "Render pipeline"),
    TaskProfile("Install Application", m2pro_engine.TaskPriority.USER_INITIATED,
                0.35, 0.0, 0.20, 0.90, 0.0, 500, 0.20, "SSD + E-Core",
                "Yoğun disk yazma"),
    TaskProfile("Spotlight Indexing", m2pro_engine.TaskPriority.UTILITY,
                0.35, 0.0, 0.30, 0.55, 0.0, 900, 0.25, "E-Core",
                "Dosya metadata tarama"),
    TaskProfile("OneDrive Sync", m2pro_engine.TaskPriority.UTILITY,
                0.20, 0.0, 0.20, 0.45, 0.0, 1200, 0.12, "E-Core",
                "Arka plan senkronizasyon"),
    TaskProfile("Thumbnail Generation", m2pro_engine.TaskPriority.UTILITY,
                0.40, 0.25, 0.35, 0.20, 0.0, 700, 0.30, "E-Core + GPU",
                "Foto/video preview"),
    TaskProfile("Mail Sync", m2pro_engine.TaskPriority.UTILITY,
                0.10, 0.0, 0.10, 0.05, 0.0, 400, 0.05, "E-Core",
                "Düşük yük"),
    TaskProfile("Backup Compression", m2pro_engine.TaskPriority.UTILITY,
                0.65, 0.0, 0.35, 0.60, 0.0, 1100, 0.45, "E-Core Cluster",
                "Arşivleme"),
    TaskProfile("Antivirus Scan", m2pro_engine.TaskPriority.BACKGROUND,
                0.45, 0.0, 0.20, 0.50, 0.0, 1300, 0.30, "E-Core",
                "Dosya tarama"),
    TaskProfile("Cache Cleanup", m2pro_engine.TaskPriority.BACKGROUND,
                0.15, 0.0, 0.10, 0.20, 0.0, 300, 0.05, "E-Core",
                "Temp silme"),
    TaskProfile("Memory Compression", m2pro_engine.TaskPriority.BACKGROUND,
                0.35, 0.0, 0.65, 0.0, 0.0, 550, 0.20, "E-Core",
                "RAM pressure çözümü"),
    TaskProfile("Disk TRIM", m2pro_engine.TaskPriority.BACKGROUND,
                0.05, 0.0, 0.05, 0.80, 0.0, 900, 0.08, "SSD Controller",
                "SSD bakım"),
    TaskProfile("Telemetry Upload", m2pro_engine.TaskPriority.BACKGROUND,
                0.05, 0.0, 0.05, 0.05, 0.0, 400, 0.03, "E-Core",
                "Log gönderme"),
    TaskProfile("Blender Render", m2pro_engine.TaskPriority.USER_INITIATED,
                0.70, 1.00, 0.80, 0.10, 0.0, 1800, 1.00, "GPU + P-Core",
                "Render workload"),
    TaskProfile("4K Video Export", m2pro_engine.TaskPriority.USER_INITIATED,
                0.75, 0.85, 0.70, 0.40, 0.0, 1500, 0.90,
                "GPU + Media Engine", "Encode"),
    TaskProfile("AAA Game Running", m2pro_engine.TaskPriority.INTERACTIVE,
                0.80, 1.00, 0.85, 0.10, 0.0, 9999, 1.00, "GPU + P-Core",
                "Realtime"),
    TaskProfile("Physics Simulation", m2pro_engine.TaskPriority.USER_INITIATED,
                1.00, 0.20, 0.55, 0.0, 0.0, 1200, 0.95, "P-Core Cluster",
                "Yoğun floating point"),
    TaskProfile("Crypto Hashing", m2pro_engine.TaskPriority.BACKGROUND,
                1.00, 0.0, 0.10, 0.0, 0.0, 9999, 1.00, "P-Core",
                "Tam CPU yük"),
    TaskProfile("Speech To Text", m2pro_engine.TaskPriority.INTERACTIVE,
                0.20, 0.0, 0.20, 0.0, 0.70, 250, 0.20, "Neural Engine",
                "Canlı transkripsiyon"),
    TaskProfile("Photo Object Detection", m2pro_engine.TaskPriority.USER_INITIATED,
                0.25, 0.10, 0.25, 0.10, 0.85, 500, 0.30, "Neural Engine",
                "Vision task"),
    TaskProfile("Live Background Blur", m2pro_engine.TaskPriority.INTERACTIVE,
                0.20, 0.35, 0.20, 0.0, 0.75, 9999, 0.45, "NE + GPU",
                "Video conference"),
    TaskProfile("OCR Document Scan", m2pro_engine.TaskPriority.USER_INITIATED,
                0.20, 0.0, 0.15, 0.05, 0.90, 300, 0.22, "Neural Engine",
                "Metin tanıma"),
    TaskProfile("Local LLM Inference", m2pro_engine.TaskPriority.USER_INITIATED,
                0.55, 0.40, 1.00, 0.30, 0.50, 1600, 0.85,
                "RAM + GPU + NE", "Büyük model çalıştırma"),
]

TASK_MAP = {task.name: task for task in TASK_LIBRARY}


def get_task_profile(name):
    if name not in TASK_MAP:
        raise KeyError(f"TaskProfile bulunamadı: {name}")
    return TASK_MAP[name]


def apply_task_profile(sim, profile, log_fn, intensity=1.0):
    """TaskProfile kaynak baskısını topolojiye yansıtır."""
    duration = max(40, int(profile.duration * intensity))
    cpu_count = max(1, int(round(1 + profile.cpu * 5)))
    e_count = max(1, int(round(1 + profile.cpu * 3)))
    gpu_count = int(round(profile.gpu * 8))
    ne_count = int(round(profile.ne * 8))
    ram_boost = max(1, int(profile.ram * 18))

    p_alus = [nid for nid in sim.get_all_node_ids()
              if "P_Core_" in sim.get_node_name(nid)
              and "ALU" in sim.get_node_name(nid)]
    e_alus = [nid for nid in sim.get_all_node_ids()
              if "E_Core_" in sim.get_node_name(nid)
              and "ALU" in sim.get_node_name(nid)]
    gpu_ids = find_all_nodes_by_type(sim, m2pro_engine.NodeType.GPU_CORE)
    ne_ids = find_all_nodes_by_type(sim, m2pro_engine.NodeType.NEURAL_ENGINE)

    cpu_targets = e_alus if "E-Core" in profile.preferred_unit and "P-Core" not in profile.preferred_unit else p_alus
    if "P-Core" in profile.preferred_unit and "E-Core" in profile.preferred_unit:
        cpu_targets = p_alus[:cpu_count] + e_alus[:e_count]

    for nid in cpu_targets[:cpu_count]:
        try:
            sim.assign_task_to_node(nid, profile.qos, duration)
            if profile.cpu > 0.6:
                sim.mark_node_dirty(nid)
        except Exception:
            pass

    if "E-Core" in profile.preferred_unit and "P-Core" not in profile.preferred_unit:
        for nid in e_alus[:e_count]:
            try:
                sim.assign_task_to_node(nid, profile.qos, duration)
            except Exception:
                pass

    for nid in gpu_ids[:gpu_count]:
        try:
            sim.assign_task_to_node(nid, profile.qos, duration)
            if profile.gpu > 0.4:
                sim.mark_node_dirty(nid)
        except Exception:
            pass

    for nid in ne_ids[:ne_count]:
        try:
            sim.assign_task_to_node(nid, profile.qos, duration)
        except Exception:
            pass

    try:
        ram_id = find_node_by_name(sim, "Unified_RAM_16GB")
        slc_id = find_node_by_name(sim, "System_Level_Cache_SLC")
        io_id = find_node_by_name(sim, "IO_Hub")
        ssd_id = find_node_by_name(sim, "NVMe_SSD")

        sim.add_load_to_node(ram_id, ram_boost)
        if profile.ram > 0.45:
            sim.mark_node_dirty(ram_id)
        sim.add_load_to_node(slc_id, max(1, int(profile.ram * 12 + profile.ne * 8)))
        sim.increase_edge_traffic(slc_id, ram_id, min(0.95, 0.10 + profile.ram * 0.60))

        if profile.ssd > 0.0:
            sim.add_load_to_node(io_id, max(1, int(profile.ssd * 10)))
            sim.add_load_to_node(ssd_id, max(1, int(profile.ssd * 12)))
            sim.increase_edge_traffic(io_id, ssd_id, min(0.95, 0.15 + profile.ssd * 0.70))
            sim.increase_edge_traffic(io_id, slc_id, min(0.95, 0.08 + profile.ssd * 0.50))

        if profile.gpu > 0.0 and gpu_ids:
            sim.increase_edge_traffic(slc_id, gpu_ids[0], min(0.95, 0.10 + profile.gpu * 0.70))
        if profile.ne > 0.0 and ne_ids:
            sim.increase_edge_traffic(slc_id, ne_ids[0], min(0.95, 0.10 + profile.ne * 0.70))
    except Exception:
        pass

    if profile.thermal > 0.0:
        try:
            sim.state.update_temperature(min(102.0, 42.0 + profile.thermal * 55.0))
            sim.state.update_gpu_temperature(min(100.0, 38.0 + profile.gpu * 45.0 + profile.thermal * 25.0))
            sim.state.gpu_memory_pressure = min(1.0, profile.gpu * 0.65 + profile.ram * 0.35)
        except Exception:
            pass

    log_fn(f"[TASK] {profile.name} → {profile.preferred_unit} | {profile.notes}")


# ============================================================================
# UI KOORDİNAT HARİTASI  —  Genişletilmiş Layout
# ============================================================================
def build_ui_mapping(sim):
    mapping = {}
    all_ids = sim.get_all_node_ids()

    # ── Paylaşımlı Birimler (Aşağıya doğru esnetildi) ───────────────
    # Y eksenindeki aralıklar (100'den 130'a) çıkarıldı ve aşağı itildi
    shared = {
        "System_Level_Cache_SLC": (600, 350),
        "Unified_RAM_16GB":       (600, 480),
        "IO_Hub":                 (600, 610),
        "NVMe_SSD":               (600, 740),
    }

    p_cores, e_cores, gpu_cores, ne_cores = {}, {}, [], []

    for nid in all_ids:
        name = sim.get_node_name(nid)
        if name in shared:
            mapping[nid] = shared[name]
            continue
        if name.startswith("P_Core_"):
            idx = int(name.split("_")[2])
            p_cores.setdefault(idx, []).append((name, nid))
        elif name.startswith("E_Core_"):
            idx = int(name.split("_")[2])
            e_cores.setdefault(idx, []).append((name, nid))
        elif name.startswith("GPU_Core_"):
            gpu_cores.append((int(name.split("_")[2]), name, nid))
        elif name.startswith("NE_Core_"):
            ne_cores.append((int(name.split("_")[2]), name, nid))

    # ── P-Core Cluster (Sol Üst - Satır aralıkları açıldı) ───────────────
    for ci, members in p_cores.items():
        col = ci % 3; row = ci // 3
        # Satır aralığı (row * 160) yerine (row * 180) yapılarak aşağı uzatıldı
        cx = 140 + col * 140; cy = 110 + row * 180
        for name, nid in members:
            if name.endswith("Core_Root"):      mapping[nid] = (cx, cy - 35)
            elif name.endswith("L2_Cache"):     mapping[nid] = (cx - 30, cy - 10)
            elif name.endswith("L1_Cache"):     mapping[nid] = (cx + 30, cy - 10)
            elif name.endswith("Register_File"):mapping[nid] = (cx, cy + 15)
            elif name.endswith("ALU_Adder"):    mapping[nid] = (cx - 30, cy + 45)
            elif name.endswith("Multiplier"):   mapping[nid] = (cx + 30, cy + 45)
            else: mapping[nid] = (cx, cy)

    # ── E-Core Cluster (Sol Alt - Blok olarak aşağı çekildi) ─────────────
    for ci, members in e_cores.items():
        col = ci % 2; row = ci // 2
        # Başlangıç Y noktası 480'den 550'ye çekildi
        cx = 210 + col * 140; cy = 550 + row * 160
        for name, nid in members:
            if name.endswith("Core_Root"):      mapping[nid] = (cx, cy - 30)
            elif name.endswith("L2_Cache"):     mapping[nid] = (cx - 25, cy - 5)
            elif name.endswith("L1_Cache"):     mapping[nid] = (cx + 25, cy - 5)
            elif name.endswith("Register_File"):mapping[nid] = (cx, cy + 20)
            elif name.endswith("ALU_General"):  mapping[nid] = (cx, cy + 45)
            else: mapping[nid] = (cx, cy)

    # ── GPU Cores (Sağ Üst - Satır aralıkları açıldı) ────────────────────
    gpu_sorted = sorted(gpu_cores, key=lambda x: x[0])
    for rank, (_, _, nid) in enumerate(gpu_sorted):
        col = rank % 5; row = rank // 5
        # Satır aralığı 55'ten 65'e çıkarıldı
        mapping[nid] = (780 + col * 55, 90 + row * 65)

    # ── Neural Engine (Sağ Alt - Blok aşağı çekildi ve esnetildi) ────────
    ne_sorted = sorted(ne_cores, key=lambda x: x[0])
    for rank, (_, _, nid) in enumerate(ne_sorted):
        col = rank % 4; row = rank // 4
        # Başlangıç Y: 420 -> 480, Satır Aralığı: 60 -> 70
        mapping[nid] = (810 + col * 60, 480 + row * 70)

    return mapping
    
    
def build_edge_set(sim):
    """Çizilecek kenarların yinelenmesiz setini döndürür: {(min,max): True}"""
    es = {}
    for nid in sim.get_all_node_ids():
        for nb in sim.get_neighbors(nid):
            es[(min(nid, nb), max(nid, nb))] = True
    return es


# ============================================================================
# AKTİF GÖREV  —  Animasyon nesnesi
# ============================================================================

class ActiveTask:
    COLORS = {
        m2pro_engine.TaskPriority.INTERACTIVE:    (255, 60,  60),
        m2pro_engine.TaskPriority.USER_INITIATED: (255, 255, 255),
        m2pro_engine.TaskPriority.UTILITY:        (100, 200, 255),
        m2pro_engine.TaskPriority.BACKGROUND:     (80,  200, 100),
    }

    def __init__(self, result, qos, label=""):
        self.route    = result
        self.qos      = qos
        self.label    = label
        self.t        = 0.0
        self.seg      = 0
        self.finished = False
        self.color    = self.COLORS.get(qos, (200, 200, 200))


# ============================================================================
# TASK ENGINE  —  OS Görev Zamanlayıcısı
# ============================================================================

class TaskEngine:
    """
    macOS GCD benzeri görev kuyruğu.
    Görevler INTERACTIVE/USER_INITIATED/UTILITY/BACKGROUND olarak sıralanır.
    En yüksek öncelikli görev her dispatch_next() çağrısında işlenir.
    """
    def __init__(self, sim):
        self.sim    = sim
        self.queues = {
            m2pro_engine.TaskPriority.INTERACTIVE:    [],
            m2pro_engine.TaskPriority.USER_INITIATED: [],
            m2pro_engine.TaskPriority.UTILITY:        [],
            m2pro_engine.TaskPriority.BACKGROUND:     [],
        }

    def enqueue(self, start_id, end_id, qos, label=""):
        self.queues[qos].append((start_id, end_id, label))

    def dispatch_next(self):
        """En yüksek öncelikli görevi kuyruktan al ve rota hesapla."""
        for qos in [m2pro_engine.TaskPriority.INTERACTIVE,
                    m2pro_engine.TaskPriority.USER_INITIATED,
                    m2pro_engine.TaskPriority.UTILITY,
                    m2pro_engine.TaskPriority.BACKGROUND]:
            if self.queues[qos]:
                start, end, label = self.queues[qos].pop(0)
                result = self.sim.find_optimal_route(start, end, qos)
                return ActiveTask(result, qos, label)
        return None

    def total_pending(self):
        return sum(len(q) for q in self.queues.values())

    def clear(self):
        for q in self.queues.values():
            q.clear()


# ============================================================================
# ARKA PLAN OS NOISE  —  Kernel Activity Simülasyonu
# ============================================================================

class OSNoiseGenerator:
    """
    Gerçek bir işletim sisteminde çekirdek her zaman arka planda
    timer interrupt, GC, disk flush, thermal management gibi işler yapar.
    Bu sınıf bu "gürültüyü" simüle eder.
    """
    def __init__(self, sim):
        self.sim     = sim
        self.enabled = True

    def tick(self):
        if not self.enabled:
            return

        all_ids = self.sim.get_all_node_ids()

        # E-Core'ları ağırlıklı seç (%70 E-Core, %30 diğer)
        e_core_ids = [nid for nid in all_ids
                      if self.sim.get_node_type(nid) == m2pro_engine.NodeType.E_CORE]
        other_ids  = [nid for nid in all_ids
                      if self.sim.get_node_type(nid) not in (
                          m2pro_engine.NodeType.UNIFIED_RAM,
                          m2pro_engine.NodeType.NVME_SSD,
                          m2pro_engine.NodeType.NEURAL_ENGINE)]

        pool = e_core_ids * 3 + other_ids  # E-Core 3× ağırlık
        if not pool:
            return

        # 3-5 düğüme rastgele yük ekle
        count = random.randint(3, 5)
        for _ in range(count):
            nid = random.choice(pool)
            load = random.uniform(0.05, 0.15)   # %5-15 yük
            cycles = random.randint(8, 25)
            # Düğümün kapasitesinin %15'ini geçme (hafif gürültü)
            max_cap = self.sim.get_node_max_capacity(nid)
            add_amt = max(1, int(max_cap * load))
            self.sim.add_load_to_node(nid, add_amt)
            # Kısa süre sonra kaldırılacak şekilde atama yap
            # (tick_simulation() bunu otomatik halleder)
            try:
                self.sim.assign_task_to_node(nid,
                    m2pro_engine.TaskPriority.BACKGROUND, cycles)
            except Exception:
                pass

        # SLC ve RAM arasına hafif trafik ekle (sürekli bellek erişimi)
        try:
            slc_id = find_node_by_name(self.sim, "System_Level_Cache_SLC")
            ram_id = find_node_by_name(self.sim, "Unified_RAM_16GB")
            self.sim.increase_edge_traffic(slc_id, ram_id,
                                           random.uniform(0.02, 0.06))
        except Exception:
            pass


# ============================================================================
# GARBAGECOLLECTİON SİMÜLATÖRÜ
# ============================================================================

class GarbageCollector:
    """
    Belirli aralıklarla RAM'deki dirty bitleri temizler.
    Bu sürede RAM yolu kısa süreliğine kilitlenir (gc_lock).
    Gerçek sistemde: mark-and-sweep GC, jemalloc, zone allocator.
    """
    def __init__(self, sim, log_fn):
        self.sim     = sim
        self.log     = log_fn
        self.running = False

    def trigger(self):
        try:
            ram_id = find_node_by_name(self.sim, "Unified_RAM_16GB")
            slc_id = find_node_by_name(self.sim, "System_Level_Cache_SLC")
        except ValueError:
            return

        self.log("🗑 GC: RAM dirty bit'leri temizleniyor (bus kısa kilitli)")
        self.sim.set_gc_lock(ram_id, True)
        self.sim.set_gc_lock(slc_id, True)
        self.sim.state.gc_active = True

        # Tüm düğümlerin is_dirty bitini temizle
        for nid in self.sim.get_all_node_ids():
            self.sim.mark_node_clean(nid)

        # 30 cycle sonra kilidi kaldır
        self.sim.assign_task_to_node(ram_id,
            m2pro_engine.TaskPriority.BACKGROUND, 30)
        self.sim.assign_task_to_node(slc_id,
            m2pro_engine.TaskPriority.BACKGROUND, 30)

        self.sim.state.gc_active = False


# ============================================================================
# SENARYO TANIMLARI
# ============================================================================

class Scenario:
    def __init__(self, name, desc, start_fn, end_fn, qos,
                 pre=None, steps=None, stress=None, task_profile=None, category=None):
        self.name       = name
        self.desc       = desc
        self.start_fn   = start_fn
        self.end_fn     = end_fn
        self.qos        = qos
        self.pre        = pre
        self.steps      = steps or []
        self.stress     = stress
        self.task_profile = task_profile
        self.category   = category or "hardware"


def os_scheduler_find_best(sim, start_id, keyword, qos):
    """
    keyword içeren tüm düğümler arasında Dijkstra ile en ucuz rotayı bul.
    En düşük maliyetli hedefe ata ve kilitle.
    """
    best_id, best_cost = None, float('inf')
    for nid in sim.get_all_node_ids():
        if keyword in sim.get_node_name(nid):
            res = sim.find_optimal_route(start_id, nid, qos)
            if res.route_found and res.total_cost < best_cost:
                best_cost, best_id = res.total_cost, nid
    if best_id is not None:
        sim.assign_task_to_node(best_id, qos, 150)
    return best_id


def build_scenarios(sim, log_fn):
    s = sim

    def s1_start(_): return find_node_by_name(s, "Unified_RAM_16GB")
    def s1_end(_):   return os_scheduler_find_best(
        s, s1_start(None), "L1_Cache", m2pro_engine.TaskPriority.USER_INITIATED)

    def s2_pre(_):
        nid = find_node_by_name(s, "P_Core_0_ALU_Adder")
        s.assign_task_to_node(nid, m2pro_engine.TaskPriority.BACKGROUND, 400)
        log_fn("[S2] EDGE: P_Core_0_ALU_Adder kilitlendi")
    def s2_start(_): return find_node_by_name(s, "Unified_RAM_16GB")
    def s2_end(_):   return os_scheduler_find_best(
        s, s2_start(None), "ALU_Adder", m2pro_engine.TaskPriority.INTERACTIVE)

    def s3_pre(_):
        s.state.update_temperature(95.0)
        log_fn("[S3] EDGE: CPU 95°C → throttling aktif")
    def s3_start(_): return find_node_by_name(s, "Unified_RAM_16GB")
    def s3_end(_):   return os_scheduler_find_best(
        s, s3_start(None), "ALU", m2pro_engine.TaskPriority.USER_INITIATED)

    def s4_pre(_):
        io = find_node_by_name(s, "IO_Hub")
        s.assign_task_to_node(io, m2pro_engine.TaskPriority.UTILITY, 700)
        log_fn("[S4] EDGE: IO Hub yoğunlaştırıldı")
    def s4_start(_): return find_node_by_name(s, "NVMe_SSD")
    def s4_end(_):   return os_scheduler_find_best(
        s, s4_start(None), "ALU", m2pro_engine.TaskPriority.UTILITY)

    def s5_pre(_):
        ram = find_node_by_name(s, "Unified_RAM_16GB")
        s.assign_task_to_node(ram, m2pro_engine.TaskPriority.BACKGROUND, 900)
        log_fn("[S5] EDGE: RAM dolu senaryosu")
    def s5_start(_): return find_node_by_name(s, "P_Core_1_L2_Cache")
    def s5_end(_):   return find_node_by_name(s, "NVMe_SSD")

    def s6_pre(_):
        log_fn("[S6] EDGE: SLC + RAM lock ve thrash baskısı")
        s.assign_task_to_node(find_node_by_name(s, "Unified_RAM_16GB"),
            m2pro_engine.TaskPriority.BACKGROUND, 1200)
        s.assign_task_to_node(find_node_by_name(s, "System_Level_Cache_SLC"),
            m2pro_engine.TaskPriority.BACKGROUND, 1200)
        for i in range(6):
            try:
                s.assign_task_to_node(find_node_by_name(s, f"P_Core_{i}_L2_Cache"),
                    m2pro_engine.TaskPriority.UTILITY, 900)
            except Exception:
                pass
    def s6_start(_): return find_node_by_name(s, "NVMe_SSD")
    def s6_end(_):   return os_scheduler_find_best(
        s, s6_start(None), "ALU", m2pro_engine.TaskPriority.BACKGROUND)

    def s7_pre(_):
        log_fn("[S7] EDGE: P-Core'lar dolu → E-Core'a kaçış")
        for i in range(3):
            try:
                s.assign_task_to_node(find_node_by_name(s, f"P_Core_{i}_ALU_Multiplier"),
                    m2pro_engine.TaskPriority.INTERACTIVE, 600)
            except Exception:
                pass
    def s7_start(_): return find_node_by_name(s, "Unified_RAM_16GB")
    def s7_end(_):   return os_scheduler_find_best(
        s, s7_start(None), "ALU", m2pro_engine.TaskPriority.USER_INITIATED)

    def s8_pre(_):
        log_fn("[S8] EDGE: KERNEL PANIC baskısı")
        s.state.update_temperature(100.0)
        s.state.update_gpu_temperature(98.0)
        all_n = s.get_all_node_ids()
        for nid in all_n:
            if random.random() < 0.60:
                s.assign_task_to_node(nid, m2pro_engine.TaskPriority.UTILITY,
                    random.randint(300, 900))
        for _ in range(50):
            u = random.choice(all_n)
            v = random.choice(all_n)
            s.increase_edge_traffic(u, v, 0.95)
    def s8_start(_): return find_node_by_name(s, "NVMe_SSD")
    def s8_end(_):   return os_scheduler_find_best(
        s, s8_start(None), "Register_File", m2pro_engine.TaskPriority.INTERACTIVE)

    def s9_pre(_):
        log_fn("[S9] EDGE: NE model çekiyor, SLC baskı altında")
        ne_ids = find_all_nodes_by_type(s, m2pro_engine.NodeType.NEURAL_ENGINE)
        for nid in ne_ids[:8]:
            s.assign_task_to_node(nid, m2pro_engine.TaskPriority.USER_INITIATED, 400)
        slc_id = find_node_by_name(s, "System_Level_Cache_SLC")
        s.add_load_to_node(slc_id, 10)
    def s9_start(_): return find_node_by_name(s, "NVMe_SSD")
    def s9_end(_):
        ne_ids = find_all_nodes_by_type(s, m2pro_engine.NodeType.NEURAL_ENGINE)
        return ne_ids[0] if ne_ids else find_node_by_name(s, "NVMe_SSD")

    def s10_pre(_):
        log_fn("[S10] EDGE: GPU dirty cache + coherency baskısı")
        gpu_ids = find_all_nodes_by_type(s, m2pro_engine.NodeType.GPU_CORE)
        for nid in gpu_ids:
            s.assign_task_to_node(nid, m2pro_engine.TaskPriority.USER_INITIATED, 500)
            s.mark_node_dirty(nid)
        s.state.update_gpu_temperature(88.0)
        s.state.gpu_memory_pressure = 0.85
    def s10_start(_):
        gpu_ids = find_all_nodes_by_type(s, m2pro_engine.NodeType.GPU_CORE)
        return gpu_ids[0] if gpu_ids else find_node_by_name(s, "Unified_RAM_16GB")
    def s10_end(_): return find_node_by_name(s, "P_Core_0_L1_Cache")

    def s11_pre(_):
        apply_task_profile(s, get_task_profile("Open Browser Tab"), log_fn, 1.0)
        apply_task_profile(s, get_task_profile("Scroll Web Page"), log_fn, 1.2)
        apply_task_profile(s, get_task_profile("Thumbnail Generation"), log_fn, 0.8)
        s.add_load_to_node(find_node_by_name(s, "Unified_RAM_16GB"), 8)
        log_fn("[S11] EDGE: 25 sekme, RAM pressure + UI compositing baskısı")
    def s11_start(_): return find_node_by_name(s, "Unified_RAM_16GB")
    def s11_end(_):   return os_scheduler_find_best(
        s, s11_start(None), "L1_Cache", m2pro_engine.TaskPriority.INTERACTIVE)

    def s12_pre(_):
        apply_task_profile(s, get_task_profile("Xcode Build Project"), log_fn, 1.0)
        apply_task_profile(s, get_task_profile("Open Browser Tab"), log_fn, 0.8)
        apply_task_profile(s, get_task_profile("Mail Sync"), log_fn, 0.6)
        log_fn("[S12] EDGE: Build + Safari + Spotify benzeri karma yük")
    def s12_start(_): return find_node_by_name(s, "Unified_RAM_16GB")
    def s12_end(_):   return os_scheduler_find_best(
        s, s12_start(None), "ALU", m2pro_engine.TaskPriority.USER_INITIATED)

    def s13_pre(_):
        apply_task_profile(s, get_task_profile("Live Background Blur"), log_fn, 1.0)
        apply_task_profile(s, get_task_profile("Speech To Text"), log_fn, 1.0)
        apply_task_profile(s, get_task_profile("Video Call Mute Toggle"), log_fn, 0.7)
        log_fn("[S13] EDGE: Zoom/Meet → NE + GPU + interactive baskısı")
    def s13_start(_): return find_node_by_name(s, "IO_Hub")
    def s13_end(_):
        ne_ids = find_all_nodes_by_type(s, m2pro_engine.NodeType.NEURAL_ENGINE)
        return ne_ids[0] if ne_ids else find_node_by_name(s, "P_Core_0_L1_Cache")

    def s14_pre(_):
        apply_task_profile(s, get_task_profile("Blender Render"), log_fn, 1.0)
        apply_task_profile(s, get_task_profile("ZIP Extract 10GB"), log_fn, 0.7)
        log_fn("[S14] EDGE: Blender render + export flush → GPU/SSD termal baskı")
    def s14_start(_):
        gpu_ids = find_all_nodes_by_type(s, m2pro_engine.NodeType.GPU_CORE)
        return gpu_ids[0] if gpu_ids else find_node_by_name(s, "Unified_RAM_16GB")
    def s14_end(_): return find_node_by_name(s, "NVMe_SSD")

    def s15_pre(_):
        apply_task_profile(s, get_task_profile("Install Application"), log_fn, 1.0)
        apply_task_profile(s, get_task_profile("Antivirus Scan"), log_fn, 1.0)
        apply_task_profile(s, get_task_profile("AAA Game Running"), log_fn, 0.9)
        log_fn("[S15] EDGE: Update + AV scan + gaming çakışması")
    def s15_start(_): return find_node_by_name(s, "NVMe_SSD")
    def s15_end(_):   return os_scheduler_find_best(
        s, s15_start(None), "ALU", m2pro_engine.TaskPriority.INTERACTIVE)

    def s16_pre(_):
        apply_task_profile(s, get_task_profile("Local LLM Inference"), log_fn, 1.0)
        apply_task_profile(s, get_task_profile("Memory Compression"), log_fn, 0.8)
        log_fn("[S16] EDGE: Local LLM → UMA / SLC / RAM doyumu")
    def s16_start(_): return find_node_by_name(s, "NVMe_SSD")
    def s16_end(_):
        ne_ids = find_all_nodes_by_type(s, m2pro_engine.NodeType.NEURAL_ENGINE)
        return ne_ids[0] if ne_ids else find_node_by_name(s, "Unified_RAM_16GB")

    def s17_pre(_):
        apply_task_profile(s, get_task_profile("Spotlight Indexing"), log_fn, 1.0)
        apply_task_profile(s, get_task_profile("Open Finder Folder"), log_fn, 0.9)
        apply_task_profile(s, get_task_profile("Thumbnail Generation"), log_fn, 0.9)
        log_fn("[S17] EDGE: Indexing + Finder preview → SSD ve E-Core baskısı")
    def s17_start(_): return find_node_by_name(s, "NVMe_SSD")
    def s17_end(_):   return os_scheduler_find_best(
        s, s17_start(None), "E_Core", m2pro_engine.TaskPriority.UTILITY)

    def s18_pre(_):
        apply_task_profile(s, get_task_profile("4K Video Export"), log_fn, 1.0)
        apply_task_profile(s, get_task_profile("Export PDF"), log_fn, 0.6)
        log_fn("[S18] EDGE: 4K export → media path + GPU + RAM baskısı")
    def s18_start(_): return find_node_by_name(s, "Unified_RAM_16GB")
    def s18_end(_):
        gpu_ids = find_all_nodes_by_type(s, m2pro_engine.NodeType.GPU_CORE)
        return gpu_ids[0] if gpu_ids else find_node_by_name(s, "NVMe_SSD")

    def s19_pre(_):
        apply_task_profile(s, get_task_profile("Backup Compression"), log_fn, 1.0)
        apply_task_profile(s, get_task_profile("OneDrive Sync"), log_fn, 0.9)
        apply_task_profile(s, get_task_profile("Disk TRIM"), log_fn, 0.8)
        log_fn("[S19] EDGE: Backup + sync + trim → uzun süreli IO contention")
    def s19_start(_): return find_node_by_name(s, "Unified_RAM_16GB")
    def s19_end(_):   return find_node_by_name(s, "NVMe_SSD")

    def s20_pre(_):
        apply_task_profile(s, get_task_profile("OCR Document Scan"), log_fn, 1.0)
        apply_task_profile(s, get_task_profile("Photo Object Detection"), log_fn, 0.8)
        apply_task_profile(s, get_task_profile("Export PDF"), log_fn, 0.8)
        log_fn("[S20] EDGE: OCR + object detection + PDF export zinciri")
    def s20_start(_): return find_node_by_name(s, "NVMe_SSD")
    def s20_end(_):
        ne_ids = find_all_nodes_by_type(s, m2pro_engine.NodeType.NEURAL_ENGINE)
        return ne_ids[0] if ne_ids else find_node_by_name(s, "P_Core_0_L1_Cache")

    return [
        Scenario("[1] Normal Load Balance",
                 "RAM→L1 — OS en boş çekirdeği seçer",
                 s1_start, s1_end, m2pro_engine.TaskPriority.USER_INITIATED,
                 steps=[
                     "kullanıcı işi alındı",
                     "uygun çekirdek araması yapılıyor",
                     "cache hiyerarşisi temiz durumda",
                     "en düşük maliyetli yol seçiliyor",
                     "iş yürütmeye alınıyor",
                 ]),
        Scenario("[2] Interrupt / Preemption",
                 "P-Core_0 kilitli → INTERACTIVE iş context switch yapar",
                 s2_start, s2_end, m2pro_engine.TaskPriority.INTERACTIVE, s2_pre,
                 steps=[
                     "interrupt geldi",
                     "runqueue yeniden değerlendiriliyor",
                     "busy core tespit edildi",
                     "preemption kararı verildi",
                     "yük daha boş hatta yönlendirildi",
                 ]),
        Scenario("[3] Thermal Throttling",
                 "95°C → P-Core %50 yavaş, Dijkstra E-Core'a yönelir",
                 s3_start, s3_end, m2pro_engine.TaskPriority.USER_INITIATED, s3_pre,
                 steps=[
                     "termal sensör okundu",
                     "CPU throttle aktif edildi",
                     "P-Core adayları maliyet cezalandırıldı",
                     "E-Core rotaları karşılaştırıldı",
                     "en ucuz E-Core yolu commit edildi",
                 ]),
        Scenario("[4] I/O Darboğazı",
                 "SSD→IO Hub→SLC→Çekirdek — yüksek gecikme",
                 s4_start, s4_end, m2pro_engine.TaskPriority.UTILITY, s4_pre,
                 steps=[
                     "disk io isteği geldi",
                     "IO Hub kuyruğu kontrol edildi",
                     "SLC üzerinden bellek yolu hesaplandı",
                     "trafik cezası eklendi",
                     "rota seçildi",
                 ]),
        Scenario("[5] Page Fault / Swap",
                 "L2 Cache → SSD — RAM doldu, OS takas başlatıyor",
                 s5_start, s5_end, m2pro_engine.TaskPriority.BACKGROUND, s5_pre,
                 steps=[
                     "page fault benzeri durum algılandı",
                     "RAM doluluk kontrol edildi",
                     "swap yolu açıldı",
                     "SSD erişimi için en ucuz hat arandı",
                     "sayfa dışarı yazıldı",
                 ]),
        Scenario("[6] Cache Thrashing",
                 "SLC+RAM kilitli, veri SSD hızına düşüyor",
                 s6_start, s6_end, m2pro_engine.TaskPriority.BACKGROUND, s6_pre,
                 steps=[
                     "cache miss zinciri tetiklendi",
                     "SLC ve RAM beklemede",
                     "çoklu core contention oluştu",
                     "dijkstra alternatif yolları eledi",
                     "en az kötü yol seçildi",
                 ]),
        Scenario("[7] Hybrid P+E Load",
                 "P-Core'lar render'da, OS E-Core'a yük dengeleme yapar",
                 s7_start, s7_end, m2pro_engine.TaskPriority.USER_INITIATED, s7_pre,
                 steps=[
                     "P-Core yoğunluğu ölçüldü",
                     "E-Core havuzu tarandı",
                     "yük dengeleme kararı verildi",
                     "termal ceza ve contention değerlendirildi",
                     "iş E-Core'a yönlendirildi",
                 ]),
        Scenario("[8] KERNEL PANIC",
                 "100°C + %60 kaos yükü + bant genişliği doyumu",
                 s8_start, s8_end, m2pro_engine.TaskPriority.INTERACTIVE, s8_pre,
                 steps=[
                     "kritik sıcaklık eşiği aşıldı",
                     "çoklu kuyruk kilitleri oluştu",
                     "bant genişliği doygunluğu tespit edildi",
                     "rota araması agresif cezalarla çalıştı",
                     "panic-benzeri durum raporlandı",
                 ]),
        Scenario("[9] AI Inference (NE)",
                 "Neural Engine SSD'den model çeker, SLC'yi kilitler",
                 s9_start, s9_end, m2pro_engine.TaskPriority.USER_INITIATED, s9_pre,
                 steps=[
                     "model ağırlıkları SSD'den okunuyor",
                     "NE önceliği verildi",
                     "SLC üzerinde DMA baskısı oluştu",
                     "memory pressure ölçüldü",
                     "en güvenli yol seçildi",
                 ]),
        Scenario("[10] GPU Compute / UMA",
                 "19 GPU çekirdeği RAM'e baskı, dirty cache +CoherencyPenalty",
                 s10_start, s10_end, m2pro_engine.TaskPriority.USER_INITIATED, s10_pre,
                 steps=[
                     "GPU compute işi başlatıldı",
                     "dirty cache işaretleri kontrol edildi",
                     "coherency maliyeti hesaba katıldı",
                     "GPU throttling sınırı tarandı",
                     "CPU okuma rotası seçildi",
                 ]),
        Scenario("[11] Chrome 25 Tab Açık",
                 "Interactive browsing + compositing + thumbnail yükü",
                 s11_start, s11_end, m2pro_engine.TaskPriority.INTERACTIVE, s11_pre,
                 steps=[
                     "tarayıcı sekmeleri geri yüklendi",
                     "UI compositing hattı ısındı",
                     "RAM baskısı ve cache miss arttı",
                     "scheduler düşük gecikmeli rota aradı",
                     "aktif sekme için hızlı yol seçildi",
                 ],
                 task_profile=get_task_profile("Open Browser Tab"),
                 category="workload"),
        Scenario("[12] Xcode Build + Safari + Spotify",
                 "Kullanıcı derleme beklerken etkileşim korunuyor",
                 s12_start, s12_end, m2pro_engine.TaskPriority.USER_INITIATED, s12_pre,
                 steps=[
                     "build kuyruğu başlatıldı",
                     "foreground sekme canlı tutuldu",
                     "arka plan sync işleri düşük önceliğe alındı",
                     "P-Core/E-Core dengesi yeniden kuruldu",
                     "derleme hattı commit edildi",
                 ],
                 task_profile=get_task_profile("Xcode Build Project"),
                 category="workload"),
        Scenario("[13] Zoom Meeting + Screen Share",
                 "Video call için NE/GPU/interactive eşzamanlı çalışır",
                 s13_start, s13_end, m2pro_engine.TaskPriority.INTERACTIVE, s13_pre,
                 steps=[
                     "kamera ve mikrofon akışı alındı",
                     "arka plan blur NE'ye yönlendirildi",
                     "speech-to-text düşük gecikmeli işlendi",
                     "GPU compositing maliyeti eklendi",
                     "çağrı kalitesini koruyan rota seçildi",
                 ],
                 task_profile=get_task_profile("Live Background Blur"),
                 category="workload"),
        Scenario("[14] Blender Render + SSD Export",
                 "Render tamamlanırken çıktı SSD'ye akıtılır",
                 s14_start, s14_end, m2pro_engine.TaskPriority.USER_INITIATED, s14_pre,
                 steps=[
                     "render tile'ları GPU'ya dağıtıldı",
                     "P-Core sahne hazırlığı yaptı",
                     "termal limitler yeniden ölçüldü",
                     "SSD export hattı açıldı",
                     "en az gecikmeli çıkış yolu seçildi",
                 ],
                 task_profile=get_task_profile("Blender Render"),
                 category="workload"),
        Scenario("[15] Update + Antivirus + Gaming",
                 "Background işler ile interactive oyun birbirini sıkıştırır",
                 s15_start, s15_end, m2pro_engine.TaskPriority.INTERACTIVE, s15_pre,
                 steps=[
                     "oyun frame budget'ı hesaplandı",
                     "arka plan tarama baskısı ölçüldü",
                     "disk erişimi yeniden önceliklendirildi",
                     "interactive rota ağır cezalarla çözüldü",
                     "oyun için en hızlı hat ayrıldı",
                 ],
                 task_profile=get_task_profile("AAA Game Running"),
                 category="workload"),
        Scenario("[16] Local LLM Inference",
                 "Büyük model UMA, GPU ve NE üzerinde baskı kurar",
                 s16_start, s16_end, m2pro_engine.TaskPriority.USER_INITIATED, s16_pre,
                 steps=[
                     "model SSD'den parçalı yüklendi",
                     "RAM ve SLC baskısı arttı",
                     "GPU/NE ortak kullanım maliyeti eklendi",
                     "memory compression devreye girdi",
                     "inferencing için güvenli rota seçildi",
                 ],
                 task_profile=get_task_profile("Local LLM Inference"),
                 category="workload"),
        Scenario("[17] Spotlight + Finder Preview",
                 "Indexing ile anlık dosya gezinmesi aynı anda çalışır",
                 s17_start, s17_end, m2pro_engine.TaskPriority.UTILITY, s17_pre,
                 steps=[
                     "dosya ağacı tarandı",
                     "preview thumbnail kuyruğu açıldı",
                     "E-Core havuzu indekslemeye ayrıldı",
                     "SSD contention cezaları uygulandı",
                     "utility hattı commit edildi",
                 ],
                 task_profile=get_task_profile("Spotlight Indexing"),
                 category="workload"),
        Scenario("[18] 4K Video Export",
                 "Encode sırasında GPU/UMA/SSD aynı anda çalışır",
                 s18_start, s18_end, m2pro_engine.TaskPriority.USER_INITIATED, s18_pre,
                 steps=[
                     "frame buffer hazırlandı",
                     "encode hattı GPU'ya açıldı",
                     "RAM bant genişliği ölçüldü",
                     "SSD yazma kuyruğu genişletildi",
                     "çıktı akışı için rota commit edildi",
                 ],
                 task_profile=get_task_profile("4K Video Export"),
                 category="workload"),
        Scenario("[19] Backup + OneDrive Sync",
                 "Uzun süreli IO ve arşivleme baskısı simüle edilir",
                 s19_start, s19_end, m2pro_engine.TaskPriority.UTILITY, s19_pre,
                 steps=[
                     "yedekleme arşivi oluşturuldu",
                     "eşzamanlı sync hattı açıldı",
                     "disk trim bakım penceresine girdi",
                     "IO darboğazı yeniden hesaplandı",
                     "en az kayıplı yazma yolu seçildi",
                 ],
                 task_profile=get_task_profile("Backup Compression"),
                 category="workload"),
        Scenario("[20] OCR + Detection + PDF",
                 "Belge işleme zinciri NE ve CPU üzerinde akar",
                 s20_start, s20_end, m2pro_engine.TaskPriority.USER_INITIATED, s20_pre,
                 steps=[
                     "görüntü SSD'den yüklendi",
                     "OCR NE'ye yönlendirildi",
                     "nesne tespiti ikinci aşamada çalıştı",
                     "çıktı PDF pipeline'ına aktarıldı",
                     "raporlama için en ucuz rota seçildi",
                 ],
                 task_profile=get_task_profile("OCR Document Scan"),
                 category="workload"),
    ]


# ============================================================================
# ANA SİMÜLATÖR SINIFI
# ============================================================================

class HardwareSimulator:

    def __init__(self):
        pygame.init()
        pygame.display.set_caption(
            "Apple M2 Pro SoC — HW/SW Co-Simulation  |  Dynamic Dijkstra v2")
        self.screen = pygame.display.set_mode((SCREEN_W, SCREEN_H))
        self.clock  = pygame.time.Clock()

        # ── Fontlar ──────────────────────────────────────────────────────────
        mono = "Menlo" if sys.platform == "darwin" else "Courier New"
        self.fL = pygame.font.SysFont(mono, 15, bold=True)
        self.fM = pygame.font.SysFont(mono, 12, bold=True)
        self.fS = pygame.font.SysFont(mono, 11)
        self.fT = pygame.font.SysFont(mono, 10)

        # ── C++ motoru ────────────────────────────────────────────────────────
        self.sim = m2pro_engine.M2ProGraph()
        self.sim.build_m2_pro_topology()
        self.sim.state.update_temperature(42.0)

        # ── Topoloji haritaları ───────────────────────────────────────────────
        self.ui_map   = build_ui_mapping(self.sim)
        self.edge_set = build_edge_set(self.sim)

        # ── Alt sistemler ─────────────────────────────────────────────────────
        self.task_engine = TaskEngine(self.sim)
        self.noise_gen   = OSNoiseGenerator(self.sim)
        self.gc          = GarbageCollector(self.sim, self.log)

        # ── Durum değişkenleri ────────────────────────────────────────────────
        self.active_tasks:   list[ActiveTask] = []
        self.selected_start: int | None = None
        self.selected_end:   int | None = None
        self.active_scenario: int | None = None
        self.cpu_temp   = 42.0
        self.gpu_temp   = 38.0
        self.noise_on   = True
        self.packet_spd = 0.007

        # ── Zamanlayıcılar ────────────────────────────────────────────────────
        self.last_tick_ms  = pygame.time.get_ticks()
        self.last_noise_ms = pygame.time.get_ticks()
        self.last_gc_ms    = pygame.time.get_ticks()

        # --- YENİ EKLENEN: Görev Geçmişi ---
        self.action_history = []

        # ── Log ───────────────────────────────────────────────────────────────
        self.log_lines: list[str] = []
        self.MAX_LOG = 14

        # ── Senaryolar ────────────────────────────────────────────────────────
        self.scenarios = build_scenarios(self.sim, self.log)

        self.log("M2 Pro SoC simülatörü başlatıldı — v2.0")
        self.log(f"Düğüm: {len(self.ui_map)}  Kenar: {len(self.edge_set)}")
        self.log("GPU(19) + Neural Engine(16) + CPU(6P+4E) hazır")
        self.log(f"Senaryo: {len(self.scenarios)}  Görev profili: {len(TASK_LIBRARY)}")

    # ── Log ──────────────────────────────────────────────────────────────────

    def log(self, msg: str):
        ts = time.strftime("%H:%M:%S")
        entry = f"[{ts}] {msg}"
        self.log_lines.append(entry)
        if len(self.log_lines) > self.MAX_LOG:
            self.log_lines.pop(0)
            
        # Hem ekrandaki panele hem de terminale yazdır:
        print(entry)

    def _node_short(self, nid):
        try:
            return self.sim.get_node_name(nid).replace("_", " ")
        except Exception:
            return f"node#{nid}"

    def _record_action_history(self, label, cost, path):
        short_path = [
            self.sim.get_node_name(n).split("_")[-1][:8]
            for n in path
        ]
        self.action_history.append({
            "label": label if label else "Sistem Görevi",
            "cost": cost,
            "path": short_path,
        })
        if len(self.action_history) > 8:
            self.action_history.pop(0)

    def _emit_steps(self, title, steps):
        self.log(f"  {title}")
        for i, step in enumerate(steps, 1):
            self.log(f"    [{i:02d}] {step}")

    def _pressure_snapshot(self, top_n=5):
        hot = []
        for nid in self.sim.get_all_node_ids():
            try:
                lr = self.sim.get_node_load_ratio(nid)
                if lr > 0.02:
                    hot.append((
                        lr,
                        self._node_short(nid),
                        self.sim.get_node_is_busy(nid),
                        self.sim.get_node_is_dirty(nid),
                    ))
            except Exception:
                pass

        hot.sort(reverse=True, key=lambda x: x[0])
        if not hot:
            return "temiz"

        return " | ".join(
            f"{name}:{lr*100:.0f}%"
            f"{' BUSY' if busy else ''}"
            f"{' DIRTY' if dirty else ''}"
            for lr, name, busy, dirty in hot[:top_n]
        )

    def _profile_summary(self, profile):
        return ("CPU:{:.0f}% GPU:{:.0f}% RAM:{:.0f}% SSD:{:.0f}% NE:{:.0f}% "
                "Thermal:{:.0f}% → {}").format(
            profile.cpu * 100,
            profile.gpu * 100,
            profile.ram * 100,
            profile.ssd * 100,
            profile.ne * 100,
            profile.thermal * 100,
            profile.preferred_unit,
        )

    # ── Senaryo çalıştırma ───────────────────────────────────────────────────

    def run_scenario(self, idx: int):
        if idx >= len(self.scenarios):
            return
        sc = self.scenarios[idx]
        self.active_scenario = idx
        self.log(f"━━ {sc.name}")
        self.log(f"  Amaç: {sc.desc}")
        self.log(f"  Ön yük görünümü: {self._pressure_snapshot()}")

        if sc.task_profile:
            self.log(f"  Görev profili: {sc.task_profile.name}")
            self.log(f"  Kaynak vektörü: {self._profile_summary(sc.task_profile)}")

        if sc.pre:
            try:
                self.log("  [00] Edge-case hazırlığı")
                sc.pre(self.sim)
            except Exception as e:
                self.log(f"[PRE HATA] {e}")

        if sc.stress:
            try:
                self.log("  [01] Stres enjeksiyonu")
                sc.stress(self.sim)
            except Exception as e:
                self.log(f"[STRESS HATA] {e}")

        try:
            start = sc.start_fn(self.sim)
            end   = sc.end_fn(self.sim)
        except Exception as e:
            self.log(f"[HATA] {e}"); return

        if start is None or end is None:
            self.log("[HATA] Başlangıç/bitiş düğümü bulunamadı"); return

        self.selected_start = start
        self.selected_end   = end
        self._emit_steps(f"OS TRACE — {sc.name}", sc.steps or [
            "syscall / interrupt entry",
            "runqueue taranıyor",
            "busy / dirty / throttling kontrolü yapılıyor",
            "Dijkstra relaxation başlıyor",
            "en ucuz yol commit ediliyor",
        ])
        self._dispatch(start, end, sc.qos, sc.name)
        self.log(f"  Son yük görünümü: {self._pressure_snapshot()}")

    def _dispatch(self, start, end, qos, label=""):
        t0      = time.perf_counter()
        result  = self.sim.find_optimal_route(start, end, qos)
        elapsed = (time.perf_counter() - t0) * 1_000_000

        task = ActiveTask(result, qos, label)
        self.active_tasks.append(task)

        if result.route_found:
            hops = " → ".join(
                self.sim.get_node_name(n).split("_")[-1]
                for n in result.path)
            self.log(f"  Rota({len(result.path)} hop): {hops[:80]}")
            self._record_action_history(label, result.total_cost, result.path)
            self.log(
                f"  Cost:{result.total_cost:.0f}cy  "
                f"Base:{result.base_cost:.0f}  "
                f"Cont:{result.contention_penalty:.0f}  "
                f"Coh:{result.coherency_penalty:.0f}  "
                f"Therm:{result.thermal_penalty:.0f}  "
                f"OS:{result.os_penalty:.0f}")
            self.log(f"  Dijkstra: {elapsed:.1f} μs"
                     + ("  ⚠ PageFault" if result.triggered_page_fault else "")
                     + ("  ⚠ CacheMiss" if result.triggered_cache_miss  else ""))
        else:
            self.log("  ⚠ Rota bulunamadı — tüm yollar kapalı olabilir")

    # ── Zamanlı alt sistem tetikleyicileri ───────────────────────────────────

    def _maybe_tick(self):
        now = pygame.time.get_ticks()
        if now - self.last_tick_ms >= TICK_INTERVAL_MS:
            freed = self.sim.tick_simulation()
            if freed:
                names = [self.sim.get_node_name(n).split("_")[-1]
                         for n in freed[:4]]
                self.log(f"  ✓ Tamamlandı: {', '.join(names)}"
                         + (f" +{len(freed)-4}" if len(freed) > 4 else ""))
                
            # --- YENİ EKLENEN KISIM: Task Queue'yu İşle ---
            # Kuyrukta bekleyen görev varsa her tick'te bir tanesini işleme al
            if self.task_engine.total_pending() > 0:
                task = self.task_engine.dispatch_next()
                if task:
                    self.active_tasks.append(task)
                    
                    if task.route.route_found:
                        self._record_action_history(
                            task.label,
                            task.route.total_cost,
                            task.route.path,
                        )
                        self.log(f"  [QUEUE] Görev başladı. Cost: {task.route.total_cost:.0f}")
                    else:
                        self.log("  [QUEUE] Rota bulunamadı.")
                else:
                    self.log("  [QUEUE] Kuyrukta dispatch edilecek görev bulunamadı.")
        # ----------------------------------------------
            
            self.last_tick_ms = now

    def _maybe_noise(self):
        now = pygame.time.get_ticks()
        if self.noise_on and now - self.last_noise_ms >= NOISE_INTERVAL_MS:
            self.noise_gen.tick()
            self.last_noise_ms = now

    def _maybe_gc(self):
        now = pygame.time.get_ticks()
        if now - self.last_gc_ms >= GC_INTERVAL_MS:
            self.gc.trigger()
            self.last_gc_ms = now

    # ── Animasyon güncelleme ─────────────────────────────────────────────────

    def _update_animations(self):
        for task in self.active_tasks:
            if task.finished: continue
            path = task.route.path
            if len(path) < 2:
                task.finished = True; continue
            task.t += self.packet_spd
            if task.t >= 1.0:
                task.t = 0.0
                task.seg += 1
                if task.seg >= len(path) - 1:
                    task.finished = True
        self.active_tasks = [t for t in self.active_tasks if not t.finished]

    # ── Olay işleyici ────────────────────────────────────────────────────────

    def _node_at(self, pos):
        mx, my = pos
        for nid, (nx, ny) in self.ui_map.items():
            try:
                nt = self.sim.get_node_type(nid)
            except Exception:
                continue
            r = R_HUB + 6 if nt in (
                m2pro_engine.NodeType.UNIFIED_RAM,
                m2pro_engine.NodeType.SLC,
                m2pro_engine.NodeType.IO_HUB,
                m2pro_engine.NodeType.NVME_SSD,
            ) else R_MED + 4
            if math.hypot(mx - nx, my - ny) <= r:
                return nid
        return None

    def _reset(self):
        for nid in self.sim.get_all_node_ids():
            self.sim.free_node(nid)
        self.sim.state.reset_stats()
        self.sim.state.update_temperature(42.0)
        self.sim.state.update_gpu_temperature(38.0)
        self.sim.state.gpu_memory_pressure = 0.0
        self.cpu_temp = 42.0
        self.gpu_temp = 38.0
        self.active_tasks.clear()
        self.task_engine.clear()
        self.selected_start = self.selected_end = None
        self.active_scenario = None
        self.log("━━ Simülasyon sıfırlandı")

    def handle_events(self) -> bool:
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT:
                return False

            elif ev.type == pygame.KEYDOWN:
                k = ev.key
                sc_keys = {
                    pygame.K_1: 0, pygame.K_2: 1, pygame.K_3: 2,
                    pygame.K_4: 3, pygame.K_5: 4, pygame.K_6: 5,
                    pygame.K_7: 6, pygame.K_8: 7, pygame.K_9: 8,
                    pygame.K_0: 9,
                }
                extra_sc_keys = {
                    pygame.K_F1: 10, pygame.K_F2: 11, pygame.K_F3: 12,
                    pygame.K_F4: 13, pygame.K_F5: 14, pygame.K_F6: 15,
                    pygame.K_F7: 16, pygame.K_F8: 17, pygame.K_F9: 18,
                    pygame.K_F10: 19,
                }
                if k in sc_keys:
                    self.run_scenario(sc_keys[k])
                elif k in extra_sc_keys:
                    self.run_scenario(extra_sc_keys[k])
                elif k == pygame.K_r:
                    self._reset()
                elif k == pygame.K_n:
                    self.noise_on = not self.noise_on
                    self.log(f"OS Noise: {'AKTİF' if self.noise_on else 'KAPALI'}")
                elif k == pygame.K_g:
                    self.gc.trigger()
                elif k in (pygame.K_PLUS, pygame.K_EQUALS):
                    self.cpu_temp = min(105.0, self.cpu_temp + 5.0)
                    self.sim.state.update_temperature(self.cpu_temp)
                    self.log(f"CPU temp: {self.cpu_temp:.0f}°C"
                             + ("THROTTLING" if self.sim.state.thermal_throttling else ""))
                elif k == pygame.K_MINUS:
                    self.cpu_temp = max(30.0, self.cpu_temp - 5.0)
                    self.sim.state.update_temperature(self.cpu_temp)
                    self.log(f"CPU temp: {self.cpu_temp:.0f}°C")
                elif k == pygame.K_RIGHTBRACKET:
                    self.gpu_temp = min(105.0, self.gpu_temp + 5.0)
                    self.sim.state.update_gpu_temperature(self.gpu_temp)
                    self.log(f"GPU temp: {self.gpu_temp:.0f}°C"
                             + (" GPU THROTTLE" if self.sim.state.gpu_throttling else ""))
                elif k == pygame.K_LEFTBRACKET:
                    self.gpu_temp = max(30.0, self.gpu_temp - 5.0)
                    self.sim.state.update_gpu_temperature(self.gpu_temp)
                    self.log(f"GPU temp: {self.gpu_temp:.0f}°C")

            elif ev.type == pygame.MOUSEBUTTONDOWN:
                nid = self._node_at(ev.pos)
                if nid is not None:
                    if ev.button == 1:
                        self.selected_start = nid
                        self.log(f"Başlangıç: {self.sim.get_node_name(nid)}")
                    elif ev.button == 3:
                        self.selected_end = nid
                        self.log(f"Bitiş: {self.sim.get_node_name(nid)}")
                        if self.selected_start is not None:
                            self.task_engine.enqueue(self.selected_start, nid, m2pro_engine.TaskPriority.USER_INITIATED, "Manual UI Task" )
                            self.log(f"Görev kuyruğa eklendi. Bekleyen: {self.task_engine.total_pending()}")
                        
                    elif ev.button == 2:
                        self.sim.assign_task_to_node(
                            nid, m2pro_engine.TaskPriority.INTERACTIVE, 50)
                        self.log(f"Görev ata: {self.sim.get_node_name(nid)}")

        return True

    # ── Çizim: Kenarlar ─────────────────────────────────────────────────────

    def _draw_edges(self):
        active_set = set()
        for task in self.active_tasks:
            if task.route.route_found:
                p = task.route.path
                for i in range(len(p)-1):
                    active_set.add((min(p[i], p[i+1]), max(p[i], p[i+1])))

        for (u, v) in self.edge_set:
            if u not in self.ui_map or v not in self.ui_map:
                continue
            p1, p2 = self.ui_map[u], self.ui_map[v]
            key = (min(u, v), max(u, v))

            if key in active_set:
                pygame.draw.line(self.screen, E_ACTIVE, p1, p2, 3)
            else:
                # Isı haritası: gerçek trafik değerini C++'tan oku
                traffic = self.sim.get_edge_traffic(u, v)
                col, w  = edge_color_width(traffic)
                pygame.draw.line(self.screen, col, p1, p2, w)

    # ── Çizim: Düğümler ──────────────────────────────────────────────────────

    def _draw_nodes(self):
        throttling     = self.sim.state.thermal_throttling
        gpu_throttling = self.sim.state.gpu_throttling

        for nid, (nx, ny) in self.ui_map.items():
            try:
                ntype    = self.sim.get_node_type(nid)
                is_busy  = self.sim.get_node_is_busy(nid)
                is_dirty = self.sim.get_node_is_dirty(nid)
                load_r   = self.sim.get_node_load_ratio(nid)
            except Exception:
                continue

            color, radius = node_style(
                ntype, is_busy, is_dirty, throttling, gpu_throttling)

            # Seçim halkası
            if nid == self.selected_start:
                pygame.draw.circle(self.screen, C_SEL_START,
                                   (nx, ny), radius + 5, 2)
            elif nid == self.selected_end:
                pygame.draw.circle(self.screen, C_SEL_END,
                                   (nx, ny), radius + 5, 2)

            # Ana daire
            pygame.draw.circle(self.screen, color, (nx, ny), radius)

            # Kapasite barı (tüm düğümlerde — küçük ama bilgilendirici)
            if load_r > 0.01:
                draw_capacity_bar(self.screen, nx, ny, radius, load_r, color)

            # Kısa etiket
            name  = self.sim.get_node_name(nid)
            short = name.split("_")[-1][:5]
            lbl   = self.fT.render(short, True, T_SECONDARY)
            self.screen.blit(lbl, (nx - lbl.get_width()//2, ny + radius + 2))

    # ── Çizim: Paket animasyonları ───────────────────────────────────────────

    def _draw_packets(self):
        for task in self.active_tasks:
            if task.finished or not task.route.route_found:
                continue
            path = task.route.path
            seg  = task.seg
            if seg >= len(path) - 1:
                continue
            u, v = path[seg], path[seg+1]
            if u not in self.ui_map or v not in self.ui_map:
                continue
            pos = lerp_pos(self.ui_map[u], self.ui_map[v], task.t)
            # Dış parlama
            gs  = pygame.Surface((22, 22), pygame.SRCALPHA)
            pygame.draw.circle(gs, (*task.color, 70), (11, 11), 11)
            self.screen.blit(gs, (pos[0]-11, pos[1]-11),
                             special_flags=pygame.BLEND_ALPHA_SDL2)
            pygame.draw.circle(self.screen, task.color, pos, 5)

    # ── Çizim: Grup etiketleri ───────────────────────────────────────────────

    def _draw_group_labels(self):
        labels = [
            (280,  20,  "P-CORE CLUSTER (6×)", C_PCORE),
            (280,  470, "E-CORE CLUSTER (4×)", C_ECORE),  # Y ekseninde aşağı çekildi
            (890,  30,  "GPU CORES (19×)", C_GPU),
            (895,  410, "NEURAL ENGINE (16×)", C_NE),     # Y ekseninde aşağı çekildi
            (600,  290, "SHARED FABRIC & I/O", C_SLC),    # Y ekseninde aşağı çekildi
        ]
        for (x, y, text, color) in labels:
            surf = self.fM.render(text, True, color)
            self.screen.blit(surf, (x - surf.get_width()//2, y))
    # ── Çizim: Dashboard ─────────────────────────────────────────────────────

    def _draw_dashboard(self):
        pr = pygame.Rect(SCREEN_W - 350, 10, 340, 820)
        pygame.draw.rect(self.screen, PANEL_BG, pr, border_radius=10)
        pygame.draw.rect(self.screen, PANEL_BORDER, pr, width=1,
                         border_radius=10)

        x  = pr.x + 12
        y  = pr.y + 10
        LH = 19

        def title(txt):
            nonlocal y
            surf = self.fM.render(txt, True, T_ACCENT)
            self.screen.blit(surf, (x, y)); y += LH + 2

        def row(label, val, vc=T_PRIMARY):
            nonlocal y
            ls = self.fS.render(label, True, T_SECONDARY)
            vs = self.fS.render(str(val), True, vc)
            self.screen.blit(ls, (x, y))
            self.screen.blit(vs, (pr.right - vs.get_width() - 12, y))
            y += LH

        def sep():
            nonlocal y
            pygame.draw.line(self.screen, PANEL_BORDER,
                             (x, y+4), (pr.right-12, y+4), 1)
            y += LH - 6

        # Başlık
        title("SYSTEM MONITOR")
        sep()

        # ── Termal ──────────────────────────────────────────────────────────
        ctemp = self.sim.state.temperature
        gtemp = self.sim.state.gpu_temperature
        ct_col = T_ALERT if ctemp >= 90 else T_PRIMARY
        gt_col = T_ALERT if gtemp >= 85 else T_PRIMARY
        thr_txt = "THROTTLE" if self.sim.state.thermal_throttling else ""
        gthr_txt = "GPU_THR"  if self.sim.state.gpu_throttling else ""

        row("CPU Temp",  f"{ctemp:.0f}°C{thr_txt}",  ct_col)
        row("GPU Temp",  f"{gtemp:.0f}°C{gthr_txt}",  gt_col)
        row("GPU Mem Pressure",
            f"{self.sim.state.gpu_memory_pressure*100:.0f}%",
            T_ALERT if self.sim.state.gpu_memory_pressure > 0.7 else T_PRIMARY)
        sep()

        # ── Birim Dolulukları ────────────────────────────────────────────────
        busy_c = sum(1 for n in self.sim.get_all_node_ids()
                     if self.sim.get_node_is_busy(n))
        total  = len(self.ui_map)
        row("Meşgul Birim", f"{busy_c} / {total}")

        # Kritik birimlerin doluluk barları
        critical = [
            ("SLC",  "System_Level_Cache_SLC"),
            ("RAM",  "Unified_RAM_16GB"),
        ]
        for label, partial in critical:
            try:
                nid = find_node_by_name(self.sim, partial)
                lr  = self.sim.get_node_load_ratio(nid)
                bw  = 140
                bx  = pr.right - bw - 12
                by  = y + 3
                pygame.draw.rect(self.screen, (40, 40, 55),
                                 (bx, by, bw, 9), border_radius=3)
                fw = int(bw * lr)
                if fw > 0:
                    fc = lerp_color((60, 200, 100), (220, 50, 50), lr)
                    pygame.draw.rect(self.screen, fc,
                                     (bx, by, fw, 9), border_radius=3)
                ls = self.fS.render(
                    f"{label} {lr*100:.0f}%", True, T_SECONDARY)
                self.screen.blit(ls, (x, y))
                y += LH
            except Exception:
                pass

        sep()

        # ── OS İstatistikleri ────────────────────────────────────────────────
        row("Context Switch",  self.sim.state.total_context_switches)
        row("Interrupt",       self.sim.state.total_interrupts)
        row("Page Fault",      self.sim.state.page_fault_count,
            T_ALERT if self.sim.state.page_fault_count > 0 else T_PRIMARY)
        avg_lat = self.sim.state.average_memory_latency()
        row("Avg Latency",     f"{avg_lat:.0f} cy")
        row("Total Cycles",
            f"{self.sim.state.total_cycles_elapsed:.0f}")
        sep()

        # ── Son 8 Görev ve Rota Geçmişi ──────────────────────────────────────
        title("TASK HISTORY (Son 8)")
        sep()

        if not self.action_history:
            row("Bekleniyor...", "", T_SECONDARY)
        else:
            for idx, action in enumerate(reversed(self.action_history)):
                lbl_surf = self.fS.render(
                    f"[{len(self.action_history)-idx}] {action['label'][:25]}",
                    True,
                    T_PRIMARY,
                )
                cost_surf = self.fS.render(
                    f"{action['cost']:.0f} cy",
                    True,
                    T_ACCENT,
                )
                self.screen.blit(lbl_surf, (x, y))
                self.screen.blit(cost_surf, (pr.right - cost_surf.get_width() - 12, y))
                y += LH

                path_str = " → ".join(action["path"])
                full_text = "Yol: " + path_str
                max_len = 48

                while full_text:
                    if len(full_text) <= max_len:
                        rs = self.fT.render(full_text, True, T_INFO)
                        self.screen.blit(rs, (x + 8, y))
                        y += LH - 2
                        break

                    split_idx = full_text.rfind(" → ", 0, max_len)
                    if split_idx == -1:
                        split_idx = max_len
                        line_text = full_text[:split_idx]
                        full_text = full_text[split_idx:].lstrip()
                    else:
                        line_text = full_text[:split_idx]
                        full_text = "  → " + full_text[split_idx + 3:]

                    rs = self.fT.render(line_text, True, T_INFO)
                    self.screen.blit(rs, (x + 8, y))
                    y += LH - 2

                y += 6
        sep()

        # ── Aktif senaryo ────────────────────────────────────────────────────
        if self.active_scenario is not None:
            sc = self.scenarios[self.active_scenario]
            ns = self.fS.render(sc.name[:36], True, T_ACCENT)
            self.screen.blit(ns, (x, y)); y += LH
            ds = self.fT.render(sc.desc[:42], True, T_SECONDARY)
            self.screen.blit(ds, (x, y)); y += LH

    # ── Ana döngü ────────────────────────────────────────────────────────────

    def run(self):
        running = True
        while running:
            running = self.handle_events()

            self._maybe_tick()
            self._maybe_noise()
            self._maybe_gc()
            self._update_animations()

            self.screen.fill(BG)

            self._draw_group_labels()
            self._draw_edges()
            self._draw_nodes()
            self._draw_packets()
            self._draw_dashboard()
    

            pygame.display.flip()
            self.clock.tick(FPS)

        pygame.quit()
        sys.exit()


# ============================================================================
# GİRİŞ NOKTASI
# ============================================================================

if __name__ == "__main__":
    HardwareSimulator().run()
