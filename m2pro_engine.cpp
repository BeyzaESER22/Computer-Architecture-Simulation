/**
 * ============================================================================
 * Apple M2 Pro SoC — Hardware-Software Co-Simulation Engine  v2.0
 * ============================================================================
 * Mimari Referans : Apple M2 Pro — 6P + 4E CPU, 19-core GPU, 16-core NE
 * Bellek          : 16 GB LPDDR5 Unified Memory (200 GB/s)
 * Önbellek        : L1(192KB I$/128KB D$) → L2(12MB P/4MB E) → SLC(24MB)
 *
 * Yeni v2 özellikleri:
 *   • GPU_CORE (19 adet) + NEURAL_ENGINE (16 adet) düğümleri
 *   • max_capacity / current_load → Contention Penalty (eksponansiyel)
 *   • is_dirty → Cache Coherency Penalty (+200 cycle)
 *   • Gelişmiş maliyet formülü:
 *       W_final = W_base + C_contention + C_coherency + C_thermal + C_os
 *   • Bandwidth saturation: trafik→1.0 iken maliyet 10× artar
 *   • UMA zorunluluğu: tüm birimler SLC üzerinden RAM'e ulaşır
 *   • add_load / remove_load API'si (OS noise için)
 *   • Page fault sayacı, cache miss sayacı, ortalama gecikme metriği
 *   • Garbage Collection kilidi simülasyonu
 * ============================================================================
 */

#include <iostream>
#include <string>
#include <vector>
#include <map>
#include <queue>
#include <limits>
#include <algorithm>
#include <stdexcept>
#include <cmath>
#include <numeric>

#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

namespace py = pybind11;


// ============================================================================
// BÖLÜM 1: ENUMLAR
// ============================================================================

/**
 * TaskPriority — macOS GCD öncelik sınıfları
 * Dijkstra relaxation'da preemption/interrupt kararı bu değere göre verilir.
 */
enum TaskPriority {
    BACKGROUND     = 0,   // E-Core tercih, kolay interrupt
    UTILITY        = 1,   // Arka plan IO, disk sync
    USER_INITIATED = 2,   // Kullanıcı tetikli (P-Core)
    INTERACTIVE    = 3    // Kritik yol — anında preempt
};

/**
 * NodeType — M2 Pro SoC donanım birimi sınıflandırması
 *
 * GPU_CORE   : Apple GPU (19 çekirdek) — compute shader, render pipeline
 * NEURAL_ENGINE: ANE (Apple Neural Engine, 16-core) — CoreML inference
 *
 * Thermal throttling: P_CORE ve GPU_CORE tiplerini etkiler.
 * Cache coherency  : GPU_CORE tarafından yazılan veriler is_dirty=true olur;
 *                    CPU bu veriyi okumak istediğinde +200 cycle CoherencyPenalty.
 */
enum NodeType {
    P_CORE,          // Performans CPU çekirdeği (3.49 GHz)
    E_CORE,          // Verimlilik CPU çekirdeği (2.42 GHz)
    ALU,             // Arithmetic Logic Unit
    REGISTER_FILE,   // Pipeline yazmaç dosyası
    L1_CACHE,        // Seviye-1 önbellek (çekirdek özel)
    L2_CACHE,        // Seviye-2 önbellek (çekirdek özel)
    SLC,             // System Level Cache — paylaşımlı (CPU+GPU+NE erişir)
    UNIFIED_RAM,     // LPDDR5 Unified Memory (CPU+GPU+NE paylaşır)
    IO_HUB,          // Thunderbolt/USB/PCIe denetleyicisi
    NVME_SSD,        // PCIe Gen 4 NVMe depolama
    GPU_CORE,        // Apple GPU compute birimi (19 çekirdek)
    NEURAL_ENGINE    // Apple Neural Engine (16-core, 15.8 TOPS)
};


// ============================================================================
// BÖLÜM 2: VERİ YAPILARI
// ============================================================================

/**
 * HardwareEdge — Donanım veri yolu (interconnect / internal bus)
 *
 * current_traffic : 0.0 (boş) → 1.0 (doygun)
 * Bandwidth saturation formülü (v2):
 *   C_traffic = W_base × sat_factor
 *   sat_factor = log1p(traffic × 9) / log(10)   →  traffic=1.0 ⟹ factor≈1.0
 *   Ek: traffic > 0.9 ⟹ lineer çarpan 10× uygulanır (hard saturation)
 */
struct HardwareEdge {
    int         target_id;
    double      base_cycle_cost;
    std::string bus_type;
    double      current_traffic;   // [0.0, 1.0]
    double      bandwidth_gbps;    // Teorik bant genişliği (GB/s) — bilgi amaçlı

    HardwareEdge(int target, double cost, std::string type, double bw = 0.0)
        : target_id(target),
          base_cycle_cost(cost),
          bus_type(std::move(type)),
          current_traffic(0.0),
          bandwidth_gbps(bw)
    {}

    void increase_traffic(double amount) {
        current_traffic = std::min(1.0, current_traffic + amount);
    }

    void decay_traffic(double decay_rate = 0.04) {
        current_traffic = std::max(0.0, current_traffic - decay_rate);
    }

    /**
     * Anlık bandwidth saturation cezasını hesapla.
     * traffic < 0.80 : logaritmik artış (gerçekçi bus modeli)
     * traffic ≥ 0.80 : eksponansiyel artış (tıkanma / saturation)
     * traffic = 1.00 : maliyet 10× olur
     */
    double compute_traffic_penalty() const {
        if (current_traffic < 0.80) {
            // Hafif trafik: log1p ölçeği
            return base_cycle_cost * (std::log1p(current_traffic * 4.0) / std::log(5.0)) * 0.5;
        } else {
            // Ağır trafik: eksponansiyel — her %10 için maliyet 2× artar
            double excess = (current_traffic - 0.80) / 0.20;  // [0,1]
            return base_cycle_cost * (0.5 + excess * excess * 9.5);  // max ~10×
        }
    }
};

// ─────────────────────────────────────────────────────────────────────────────

/**
 * Task — çekirdeğin zamanlayıcısına giren iş birimi
 *
 * Bilgisayar mühendisliği derslerinde önce süreç/thread PCB mantığı öğretilir,
 * sonra "ready queue + running task" ayrımı gösterilir. Buradaki Task yapısı,
 * o yaklaşımın sadeleştirilmiş halidir.
 */
struct Task {
    int          id;
    TaskPriority priority;
    int          remaining_cycles;

    Task()
        : id(-1), priority(BACKGROUND), remaining_cycles(0)
    {}

    Task(int _id, TaskPriority _priority, int _remaining_cycles)
        : id(_id), priority(_priority),
          remaining_cycles(std::max(0, _remaining_cycles))
    {}
};

struct SchedulerTickResult {
    bool task_completed;
    bool context_switched;
    bool became_idle;

    SchedulerTickResult()
        : task_completed(false), context_switched(false), became_idle(false)
    {}
};

/**
 * HardwareNode — Fiziksel donanım birimi
 *
 * v3 scheduler eklentileri:
 *   ready_queue          : Ready state'teki görevler
 *   current_active_task  : Şu an CPU üzerinde çalışan görev
 *   time_slice_used      : Aktif görevin mevcut quantum içinde harcadığı süre
 *   time_quantum         : Round Robin zaman dilimi
 *
 * Bu tasarım derslerde tipik olarak şu 4 adımla anlatılır:
 *   1. Admit   : Yeni işi ready_queue'ya al
 *   2. Dispatch: Kuyruğun başındaki işi CPU'ya ver
 *   3. Run     : Her tick'te 1 cycle ilerlet
 *   4. Preempt : Quantum dolduysa görevi kuyruğun sonuna at
 */
struct HardwareNode {
    int         id;
    std::string name;
    NodeType    type;

    // ── OS / Mimari Durum Değişkenleri ───────────────────────────────────────
    bool         is_busy;
    TaskPriority current_priority;
    int          remaining_cycles;

    // ── Round Robin scheduler durumu ─────────────────────────────────────────
    std::queue<Task> ready_queue;
    Task             current_active_task;
    bool             has_active_task;
    int              time_slice_used;
    int              time_quantum;

    // ── v2: Kapasite ve Yük ──────────────────────────────────────────────────
    int    max_capacity;     // Maksimum gözlemlenen iş baskısı
    int    current_load;     // Task queue + harici OS yükü (0 → max_capacity)
    int    external_load;    // GC / OS noise gibi scheduler dışı yük
    bool   is_dirty;         // Cache coherency — kirli veri biti
    double node_temperature; // Düğüme özgü sıcaklık (°C)
    bool   gc_locked;        // Garbage collection süresince kilitli mi?

    // Default (std::map için)
    HardwareNode()
        : id(-1), name(""), type(ALU),
          is_busy(false), current_priority(BACKGROUND), remaining_cycles(0),
          current_active_task(), has_active_task(false),
          time_slice_used(0), time_quantum(10),
          max_capacity(1), current_load(0), external_load(0), is_dirty(false),
          node_temperature(40.0), gc_locked(false)
    {}

    HardwareNode(int _id, std::string _name, NodeType _type,
                 int _max_cap = 1, int _time_quantum = 10)
        : id(_id), name(std::move(_name)), type(_type),
          is_busy(false), current_priority(BACKGROUND), remaining_cycles(0),
          current_active_task(), has_active_task(false),
          time_slice_used(0), time_quantum(std::max(1, _time_quantum)),
          max_capacity(_max_cap), current_load(0), external_load(0),
          is_dirty(false), node_temperature(40.0), gc_locked(false)
    {}

    void sync_observable_state() {
        const int runnable_task_count =
            static_cast<int>(ready_queue.size()) + (has_active_task ? 1 : 0);

        current_load = std::min(max_capacity, external_load + runnable_task_count);
        is_busy      = (current_load > 0);

        if (has_active_task) {
            current_priority = current_active_task.priority;
            remaining_cycles = current_active_task.remaining_cycles;
        } else {
            current_priority = BACKGROUND;
            remaining_cycles = 0;
            time_slice_used  = 0;
        }
    }

    void dispatch_next_task() {
        if (has_active_task || ready_queue.empty()) {
            sync_observable_state();
            return;
        }

        current_active_task = ready_queue.front();
        ready_queue.pop();
        has_active_task  = true;
        time_slice_used  = 0;
        sync_observable_state();
    }

    void set_time_quantum(int quantum) {
        time_quantum = std::max(1, quantum);
        if (time_slice_used > time_quantum) time_slice_used = time_quantum;
        sync_observable_state();
    }

    int ready_queue_size() const {
        return static_cast<int>(ready_queue.size());
    }

    // ── Yük Oranı ─────────────────────────────────────────────────────────────
    double load_ratio() const {
        if (max_capacity <= 0) return 0.0;
        return static_cast<double>(current_load) / static_cast<double>(max_capacity);
    }

    bool is_overloaded() const { return load_ratio() >= 0.80; }
    bool is_full()       const { return current_load >= max_capacity; }

    // ── Yük Ekleme / Çıkarma ─────────────────────────────────────────────────
    void add_load(int amount = 1) {
        external_load = std::max(0, external_load + amount);
        sync_observable_state();
    }

    void remove_load(int amount = 1) {
        external_load = std::max(0, external_load - amount);
        sync_observable_state();
    }

    // ── Görev Atama (eski API — geriye uyumluluk) ────────────────────────────
    void assign_task(TaskPriority priority, int cycles, int task_id) {
        if (cycles <= 0)
            throw std::invalid_argument("Task cycle sayisi sifirdan buyuk olmali.");

        Task task(task_id, priority, cycles);

        if (!has_active_task) {
            current_active_task = task;
            has_active_task     = true;
            time_slice_used     = 0;
        } else {
            ready_queue.push(task);
        }

        sync_observable_state();
    }

    void free_unit() {
        std::queue<Task> empty_queue;
        ready_queue.swap(empty_queue);

        is_busy            = false;
        current_priority   = BACKGROUND;
        remaining_cycles   = 0;
        current_active_task= Task();
        has_active_task    = false;
        time_slice_used    = 0;
        current_load       = 0;
        external_load      = 0;
        is_dirty           = false;
        gc_locked          = false;
    }

    // ── Simülasyon Tick ──────────────────────────────────────────────────────
    SchedulerTickResult tick() {
        SchedulerTickResult result;
        const bool was_busy = is_busy;

        if (!has_active_task || gc_locked) {
            sync_observable_state();
            result.became_idle = (was_busy && !has_active_task && external_load == 0);
            return result;
        }

        current_active_task.remaining_cycles--;
        time_slice_used++;

        if (current_active_task.remaining_cycles <= 0) {
            result.task_completed = true;
            has_active_task       = false;
            current_active_task   = Task();
            time_slice_used       = 0;
            dispatch_next_task();
            result.became_idle = (was_busy && !has_active_task && external_load == 0);
            sync_observable_state();
            return result;
        }

        if (time_slice_used >= time_quantum) {
            if (!ready_queue.empty()) {
                ready_queue.push(current_active_task);
                current_active_task = ready_queue.front();
                ready_queue.pop();
                result.context_switched = true;
            }
            time_slice_used = 0;
        }

        sync_observable_state();
        return result;
    }

    /**
     * Contention Penalty hesabı — Maliyet Formülü:
     *   C_contention = W_base × (load_ratio²) × CONTENTION_K
     *
     * load_ratio < 0.80 : düşük ceza (hafif çekişme)
     * load_ratio ≥ 0.80 : eksponansiyel artış (tıkanma)
     *
     * Gerçek işlemci: ROB (Re-Order Buffer) dolduğunda IPC düşer,
     * bu durum burada Contention Penalty ile modellenir.
     */
    double compute_contention_penalty(double w_base) const {
        constexpr double CONTENTION_K = 8.0;
        double ratio = load_ratio();
        if (ratio < 0.80) {
            return w_base * ratio * ratio * CONTENTION_K * 0.3;
        } else {
            // %80 üzeri: eksponansiyel bölge
            double excess = (ratio - 0.80) / 0.20;
            return w_base * (0.3 * 0.64 * CONTENTION_K +
                             excess * excess * CONTENTION_K * 5.0);
        }
    }
};

// ─────────────────────────────────────────────────────────────────────────────

/**
 * SystemState — Global sistem durumu
 *
 * v2 eklentileri:
 *   cache_miss_count     : L1/L2 miss olayı sayısı
 *   page_fault_count     : RAM → SSD swap olayı sayısı
 *   total_latency_sum    : Tüm tamamlanan rotaların toplam cycle maliyeti
 *   completed_routes     : Tamamlanan rota sayısı (ortalama gecikme için)
 *   gc_active            : Garbage Collection aktif mi? (RAM yolu kilitli)
 *   gpu_memory_pressure  : GPU bellek baskısı (0.0–1.0)
 */
struct SystemState {
    // ── Termal ──────────────────────────────────────────────────────────────
    double temperature;           // Genel sistem sıcaklığı (°C)
    double gpu_temperature;       // GPU özelinde sıcaklık
    bool   thermal_throttling;    // ≥90°C → P-Core+GPU %50 yavaşlar
    bool   gpu_throttling;        // GPU ≥85°C → GPU yolları yavaşlar

    // ── Bus ve Bellek ────────────────────────────────────────────────────────
    double total_bus_load;        // Genel sistem bus yoğunluğu [0,1]
    double gpu_memory_pressure;   // GPU bellek baskısı [0,1]
    bool   gc_active;             // GC süresince RAM yolu kısıtlı

    // ── OS İstatistikleri ────────────────────────────────────────────────────
    int    total_context_switches;
    int    total_interrupts;
    double total_cycles_elapsed;

    // ── v2 Metrikleri ────────────────────────────────────────────────────────
    int    cache_miss_count;      // L1/L2 miss sayısı
    int    page_fault_count;      // RAM doluşu → SSD swap sayısı
    double total_latency_sum;     // Tamamlanan rotaların toplam maliyeti
    int    completed_routes;      // Tamamlanan rota sayısı

    SystemState()
        : temperature(42.0), gpu_temperature(38.0),
          thermal_throttling(false), gpu_throttling(false),
          total_bus_load(0.0), gpu_memory_pressure(0.0), gc_active(false),
          total_context_switches(0), total_interrupts(0),
          total_cycles_elapsed(0.0),
          cache_miss_count(0), page_fault_count(0),
          total_latency_sum(0.0), completed_routes(0)
    {}

    // Sıcaklık güncelle — Apple SMC mantığı
    void update_temperature(double cpu_temp) {
        temperature        = cpu_temp;
        thermal_throttling = (temperature >= 90.0);
    }

    void update_gpu_temperature(double g_temp) {
        gpu_temperature = g_temp;
        gpu_throttling  = (gpu_temperature >= 85.0);
    }

    // Ortalama bellek gecikmesi (cycle)
    double average_memory_latency() const {
        if (completed_routes == 0) return 0.0;
        return total_latency_sum / static_cast<double>(completed_routes);
    }

    void reset_stats() {
        total_context_switches = 0;
        total_interrupts       = 0;
        total_cycles_elapsed   = 0.0;
        cache_miss_count       = 0;
        page_fault_count       = 0;
        total_latency_sum      = 0.0;
        completed_routes       = 0;
    }
};


// ============================================================================
// BÖLÜM 3: ROTA SONUCU
// ============================================================================

/**
 * RouteResult — find_optimal_route dönüş tipi (v2 genişletilmiş)
 *
 * v2 eklentileri:
 *   contention_penalty  : Düğüm doluluk çekişmesinden gelen maliyet
 *   coherency_penalty   : Cache coherency (is_dirty) maliyeti
 *   miss_chain          : L1→L2→SLC→RAM→SSD miss zinciri bilgisi
 */
struct RouteResult {
    std::vector<int> path;
    double total_cost;
    double base_cost;
    double traffic_penalty;
    double thermal_penalty;
    double os_penalty;
    double contention_penalty;  // v2
    double coherency_penalty;   // v2
    bool   route_found;
    bool   triggered_page_fault; // v2 — rota SSD'ye uzandıysa
    bool   triggered_cache_miss; // v2 — L1 miss zinciri oluştuysa

    RouteResult()
        : total_cost(0), base_cost(0), traffic_penalty(0),
          thermal_penalty(0), os_penalty(0),
          contention_penalty(0), coherency_penalty(0),
          route_found(false),
          triggered_page_fault(false), triggered_cache_miss(false)
    {}
};


// ============================================================================
// BÖLÜM 4: M2ProGraph — Ana Motor
// ============================================================================

class M2ProGraph {
public:
    SystemState state;
    std::map<int, HardwareNode>              nodes;
    std::map<int, std::vector<HardwareEdge>> adjacency_list;
    int next_node_id = 0;
    int next_task_id = 0;
    int scheduler_time_quantum = 10;

    // ── Topoloji Yardımcıları ────────────────────────────────────────────────

    /**
     * Düğüm ekle — max_capacity parametresi eklendi (v2)
     * Kapasite referansları (gerçek M2 Pro değerleri):
     *   ALU         : 1  (tek execution port)
     *   L1_CACHE    : 2  (dual-ported L1)
     *   L2_CACHE    : 4  (4-way access)
     *   SLC         : 16 (yüksek eş zamanlılık)
     *   UNIFIED_RAM : 32 (UMA — tüm birimler erişir)
     *   GPU_CORE    : 8  (SIMD genişliği)
     *   NEURAL_ENGINE: 4 (ANE dispatch queue)
     */
    int add_node(const std::string& name, NodeType type, int max_cap = 1) {
        int id    = next_node_id++;
        nodes[id] = HardwareNode(id, name, type, max_cap, scheduler_time_quantum);
        return id;
    }

    void add_edge(int source, int target, double cycle_cost,
                  const std::string& bus_type,
                  bool bidirectional = true,
                  double bandwidth_gbps = 0.0)
    {
        adjacency_list[source].emplace_back(target, cycle_cost, bus_type, bandwidth_gbps);
        if (bidirectional)
            adjacency_list[target].emplace_back(source, cycle_cost, bus_type, bandwidth_gbps);
    }

    // ── Topoloji İnşası ───────────────────────────────────────────────────────

    /**
     * M2 Pro SoC Topolojisini İnşa Et (v2 — GPU + Neural Engine eklendi)
     *
     * UMA Zorunluluğu:
     *   CPU → Core Root → SLC → RAM
     *   GPU → GPU Cluster → SLC → RAM
     *   NE  → NE Root    → SLC → RAM
     *   IO  → IO Hub     → SLC → RAM
     *
     * Tüm birimler RAM'e SLC üzerinden ulaşmak zorundadır.
     * Bu kural, gerçek Apple Silicon UMA mimarisini yansıtır.
     */
    void build_m2_pro_topology() {
        next_node_id = 0;
        next_task_id = 0;
        nodes.clear();
        adjacency_list.clear();

        // ── 1. Paylaşımlı Sistem Birimleri ───────────────────────────────────
        // max_capacity: gerçek M2 Pro DRAM bandwidth'e dayanarak normalize edildi
        int id_ram = add_node("Unified_RAM_16GB",       UNIFIED_RAM, 32);
        int id_slc = add_node("System_Level_Cache_SLC", SLC,         16);
        int id_io  = add_node("IO_Hub",                 IO_HUB,       4);
        int id_ssd = add_node("NVMe_SSD",               NVME_SSD,     2);

        // SLC ↔ RAM: LPDDR5 200 GB/s — 120 cycle latency
        add_edge(id_slc, id_ram, 120.0, "Memory_Bus",        true, 200.0);
        // IO Hub ↔ SLC: Apple Fabric — 50 cycle
        add_edge(id_io,  id_slc,  50.0, "Fabric_Interconnect", true, 50.0);
        // IO Hub ↔ SSD: PCIe Gen 4 × 4 — 7 GB/s, ~2000 cycle latency
        add_edge(id_io,  id_ssd, 2000.0, "PCIe_Gen4_Bus",    true,  7.0);

        // ── 2. CPU Performans Çekirdekleri (P-Core × 6) ───────────────────────
        for (int i = 0; i < 6; i++) {
            const std::string pfx = "P_Core_" + std::to_string(i) + "_";

            int id_core    = add_node(pfx + "Core_Root",    P_CORE,       2);
            int id_l2      = add_node(pfx + "L2_Cache",     L2_CACHE,     4);
            int id_l1      = add_node(pfx + "L1_Cache",     L1_CACHE,     2);
            int id_rf      = add_node(pfx + "Register_File",REGISTER_FILE,1);
            int id_alu_add = add_node(pfx + "ALU_Adder",    ALU,          1);
            int id_alu_mul = add_node(pfx + "ALU_Multiplier",ALU,         1);

            // P-Core → SLC: Cluster Interconnect (40 cycle, 100 GB/s)
            add_edge(id_core, id_slc,    40.0, "Cluster_Interconnect", true, 100.0);
            add_edge(id_core, id_l2,      5.0, "Core_L2_Bridge");
            add_edge(id_l2,   id_l1,     10.0, "L1_L2_Bus");
            add_edge(id_l1,   id_rf,      3.0, "Load_Store_Bus");
            // Pipeline: tek yönlü (Decode→Execute→Writeback)
            add_edge(id_rf,      id_alu_add, 1.0, "Execution_Bus",  false);
            add_edge(id_alu_add, id_rf,      1.0, "Writeback_Bus",  false);
            add_edge(id_rf,      id_alu_mul, 3.0, "Execution_Bus",  false);
            add_edge(id_alu_mul, id_rf,      3.0, "Writeback_Bus",  false);
        }

        // ── 3. CPU Verimlilik Çekirdekleri (E-Core × 4) ──────────────────────
        for (int i = 0; i < 4; i++) {
            const std::string pfx = "E_Core_" + std::to_string(i) + "_";

            int id_core = add_node(pfx + "Core_Root",    E_CORE,       1);
            int id_l2   = add_node(pfx + "L2_Cache",     L2_CACHE,     2);
            int id_l1   = add_node(pfx + "L1_Cache",     L1_CACHE,     1);
            int id_rf   = add_node(pfx + "Register_File",REGISTER_FILE,1);
            int id_alu  = add_node(pfx + "ALU_General",  ALU,          1);

            // E-Core → SLC: daha yavaş bağlantı (50 cycle, 60 GB/s)
            add_edge(id_core, id_slc,   50.0, "Cluster_Interconnect", true, 60.0);
            add_edge(id_core, id_l2,     5.0, "Core_L2_Bridge");
            add_edge(id_l2,   id_l1,    12.0, "L1_L2_Bus");
            add_edge(id_l1,   id_rf,     4.0, "Load_Store_Bus");
            add_edge(id_rf,   id_alu,    2.0, "Execution_Bus",  false);
            add_edge(id_alu,  id_rf,     2.0, "Writeback_Bus",  false);
        }

        // ── 4. GPU Çekirdekleri (GPU_CORE × 19) ──────────────────────────────
        // M2 Pro GPU: 19 çekirdek, her biri 128 SIMD lane
        // GPU → SLC bağlantısı: Apple Fabric üzerinden 55 cycle
        // GPU çekirdekleri kendi aralarında GPU_Fabric ile bağlı
        // Her GPU çekirdeğinin max_capacity=8 (SIMD warps)
        for (int i = 0; i < 19; i++) {
            const std::string pfx = "GPU_Core_" + std::to_string(i) + "_";

            int id_gpu = add_node(pfx + "Compute_Unit", GPU_CORE, 8);

            // GPU → SLC: UMA zorunluluğu — GPU bellek erişimi SLC üzerinden
            // Latency: 55 cycle (CPU'dan biraz yüksek — farklı fabric port)
            add_edge(id_gpu, id_slc, 55.0, "GPU_Fabric_Interconnect", true, 150.0);

            // GPU komşuları arası: texture/render veri paylaşımı (10 cycle)
            if (i > 0) {
                // Önceki GPU çekirdeğiyle bağ (tile rendering pipeline)
                // GPU ID'leri topoloji inşasında dinamik olarak hesaplanır
                // Bu kenar "GPU internal mesh" i modeller
                int prev_gpu_id = next_node_id - 2; // bir önceki GPU_Compute_Unit
                // Not: add_node çağrısı next_node_id'yi artırdı, geri al:
                prev_gpu_id = nodes.rbegin()->first - 1;
                // Güvenli erişim: sadece aynı tip düğümler
                if (nodes.count(prev_gpu_id) &&
                    nodes.at(prev_gpu_id).type == GPU_CORE) {
                    add_edge(id_gpu, prev_gpu_id, 10.0,
                             "GPU_Internal_Mesh", true, 300.0);
                }
            }
        }

        // ── 5. Neural Engine (NEURAL_ENGINE × 16) ────────────────────────────
        // M2 Pro ANE: 16-core, 15.8 TOPS
        // NE → SLC: 45 cycle (ANE CoreML DMA yolu)
        // NE çekirdekleri kendi aralarında NE_Interconnect ile bağlı
        int first_ne_id = -1;
        for (int i = 0; i < 16; i++) {
            const std::string pfx = "NE_Core_" + std::to_string(i) + "_";
            int id_ne = add_node(pfx + "Inference_Unit", NEURAL_ENGINE, 4);

            // NE → SLC: CoreML DMA — 45 cycle (ANE özel DMA yolu)
            add_edge(id_ne, id_slc, 45.0, "ANE_DMA_Bus", true, 80.0);

            // NE çekirdekleri arası bağ (pipeline parallelism)
            if (i == 0) {
                first_ne_id = id_ne;
            } else if (first_ne_id >= 0) {
                // Her NE çekirdeği ilk NE çekirdeğine bağlı (star topoloji)
                add_edge(id_ne, first_ne_id, 5.0, "NE_Internal_Bus", true, 50.0);
            }
        }

        std::cout << "[M2ProGraph v2] Topoloji insa edildi."
                  << "  Dugum: "  << nodes.size()
                  << "  Kenar: "  << count_edges()
                  << std::endl;
    }

    // ── Graf Sorgulama ───────────────────────────────────────────────────────

    int count_edges() const {
        int total = 0;
        for (auto const& [id, edges] : adjacency_list)
            total += static_cast<int>(edges.size());
        return total;
    }

    std::string get_node_name(int id) const {
        auto it = nodes.find(id);
        if (it == nodes.end())
            throw std::out_of_range("Node ID bulunamadi: " + std::to_string(id));
        return it->second.name;
    }

    NodeType get_node_type(int id) const {
        auto it = nodes.find(id);
        if (it == nodes.end())
            throw std::out_of_range("Node ID bulunamadi: " + std::to_string(id));
        return it->second.type;
    }

    bool get_node_is_busy(int id) const {
        auto it = nodes.find(id);
        return (it != nodes.end()) ? it->second.is_busy : false;
    }

    int get_node_priority(int id) const {
        auto it = nodes.find(id);
        return (it != nodes.end()) ? static_cast<int>(it->second.current_priority) : -1;
    }

    int get_node_active_task_id(int id) const {
        auto it = nodes.find(id);
        if (it == nodes.end() || !it->second.has_active_task) return -1;
        return it->second.current_active_task.id;
    }

    int get_node_ready_queue_size(int id) const {
        auto it = nodes.find(id);
        return (it != nodes.end()) ? it->second.ready_queue_size() : 0;
    }

    int get_node_time_slice_used(int id) const {
        auto it = nodes.find(id);
        return (it != nodes.end()) ? it->second.time_slice_used : 0;
    }

    int get_node_time_quantum(int id) const {
        auto it = nodes.find(id);
        return (it != nodes.end()) ? it->second.time_quantum : scheduler_time_quantum;
    }

    // v2: yük oranı [0.0, 1.0]
    double get_node_load_ratio(int id) const {
        auto it = nodes.find(id);
        return (it != nodes.end()) ? it->second.load_ratio() : 0.0;
    }

    // v2: is_dirty
    bool get_node_is_dirty(int id) const {
        auto it = nodes.find(id);
        return (it != nodes.end()) ? it->second.is_dirty : false;
    }

    // v2: max_capacity
    int get_node_max_capacity(int id) const {
        auto it = nodes.find(id);
        return (it != nodes.end()) ? it->second.max_capacity : 1;
    }

    // v2: current_load
    int get_node_current_load(int id) const {
        auto it = nodes.find(id);
        return (it != nodes.end()) ? it->second.current_load : 0;
    }

    // v2: düğümün sıcaklığı
    double get_node_temperature(int id) const {
        auto it = nodes.find(id);
        return (it != nodes.end()) ? it->second.node_temperature : 0.0;
    }

    std::vector<int> get_all_node_ids() const {
        std::vector<int> ids;
        ids.reserve(nodes.size());
        for (auto const& [id, _] : nodes) ids.push_back(id);
        return ids;
    }

    std::vector<int> get_neighbors(int node_id) const {
        std::vector<int> result;
        auto it = adjacency_list.find(node_id);
        if (it == adjacency_list.end()) return result;
        for (auto const& e : it->second)
            result.push_back(e.target_id);
        return result;
    }

    // v2: kenar trafik değerini döndür (Pygame renk için)
    double get_edge_traffic(int source, int target) const {
        auto it = adjacency_list.find(source);
        if (it == adjacency_list.end()) return 0.0;
        for (auto const& e : it->second)
            if (e.target_id == target) return e.current_traffic;
        return 0.0;
    }

    // ── Görev Yönetimi ───────────────────────────────────────────────────────

    void assign_task_to_node(int node_id, TaskPriority priority, int cycles) {
        auto it = nodes.find(node_id);
        if (it == nodes.end())
            throw std::out_of_range("Node ID bulunamadi: " + std::to_string(node_id));
        it->second.assign_task(priority, cycles, next_task_id++);
    }

    void set_time_quantum(int quantum) {
        if (quantum <= 0)
            throw std::invalid_argument("Time quantum sifirdan buyuk olmali.");

        scheduler_time_quantum = quantum;
        for (auto& [id, node] : nodes)
            node.set_time_quantum(quantum);
    }

    int get_time_quantum() const {
        return scheduler_time_quantum;
    }

    void free_node(int node_id) {
        auto it = nodes.find(node_id);
        if (it != nodes.end()) it->second.free_unit();
    }

    // v2: yük ekle / çıkar (OS noise ve GC için)
    void add_load_to_node(int node_id, int amount = 1) {
        auto it = nodes.find(node_id);
        if (it != nodes.end()) it->second.add_load(amount);
    }

    void remove_load_from_node(int node_id, int amount = 1) {
        auto it = nodes.find(node_id);
        if (it != nodes.end()) it->second.remove_load(amount);
    }

    // v2: is_dirty bit yönetimi (GPU write → CPU read senaryosu)
    void mark_node_dirty(int node_id) {
        auto it = nodes.find(node_id);
        if (it != nodes.end()) it->second.is_dirty = true;
    }

    void mark_node_clean(int node_id) {
        auto it = nodes.find(node_id);
        if (it != nodes.end()) it->second.is_dirty = false;
    }

    // v2: düğüm sıcaklığını güncelle
    void set_node_temperature(int node_id, double temp) {
        auto it = nodes.find(node_id);
        if (it != nodes.end()) it->second.node_temperature = temp;
    }

    // v2: GC kilidi
    void set_gc_lock(int node_id, bool locked) {
        auto it = nodes.find(node_id);
        if (it != nodes.end()) {
            it->second.gc_locked = locked;
            if (locked) it->second.add_load(1);
            else        it->second.remove_load(1);
        }
    }

    // ── Bandwidth Yönetimi ───────────────────────────────────────────────────

    void increase_edge_traffic(int source, int target, double amount = 0.1) {
        auto it = adjacency_list.find(source);
        if (it == adjacency_list.end()) return;
        for (auto& edge : it->second)
            if (edge.target_id == target) { edge.increase_traffic(amount); break; }
    }

    void decay_all_traffic(double decay_rate = 0.04) {
        for (auto& [id, edges] : adjacency_list)
            for (auto& edge : edges)
                edge.decay_traffic(decay_rate);
    }

    // ── Simülasyon Tick ──────────────────────────────────────────────────────

    std::vector<int> tick_simulation() {
        std::vector<int> freed;
        for (auto& [id, node] : nodes) {
            SchedulerTickResult tick_result = node.tick();

            // Timer interrupt sadece gerçekten başka bir göreve geçildiyse sayılır.
            if (tick_result.context_switched) {
                state.total_context_switches++;
                state.total_interrupts++;
            }

            if (tick_result.became_idle) freed.push_back(id);
        }

        decay_all_traffic();
        state.total_cycles_elapsed++;
        return freed;
    }

    // ── Dinamik Dijkstra (v2 — Gelişmiş Maliyet Formülü) ────────────────────

    /**
     * find_optimal_route — v2 Maliyet Formülü:
     *
     *   W_final = W_base
     *           + C_traffic      (bandwidth saturation — logaritmik/eksponansiyel)
     *           + C_contention   (düğüm doluluk çekişmesi — W_base × ratio²)
     *           + C_coherency    (is_dirty → +200 cycle — RAM flush maliyeti)
     *           + C_thermal      (P_CORE/GPU_CORE, throttling aktifse × 1.5)
     *           + C_os           (context switch +500 | queue wait +10000)
     *
     * UMA zorunluluğu: GPU/NE başlangıçlı rotalar SLC üzerinden geçmek zorunda.
     * Bu, gerçek Apple Silicon fabric routing'i simüle eder.
     *
     * Miss zinciri tespiti:
     *   Rota L1→L2→SLC→RAM→SSD sırasını izliyorsa cache_miss ve
     *   page_fault sayaçları güncellenir.
     */
    RouteResult find_optimal_route(int start_id, int end_id,
                                   TaskPriority task_qos)
    {
        // ── Sabitler (gerçek Apple Silicon ölçümleri referanslı) ─────────────
        constexpr double CONTEXT_SWITCH_PENALTY = 500.0;   // ~400-600 cycle
        constexpr double QUEUE_WAIT_PENALTY     = 10000.0; // etkin yolu kapatır
        constexpr double COHERENCY_PENALTY      = 200.0;   // RAM flush + invalidate
        constexpr double GC_LOCK_PENALTY        = 800.0;   // GC süresi bekleme

        RouteResult result;
        const double INF = std::numeric_limits<double>::infinity();

        // ── Min-heap: {maliyet, node_id} ─────────────────────────────────────
        using PQ = std::pair<double, int>;
        std::priority_queue<PQ, std::vector<PQ>, std::greater<PQ>> pq;

        std::map<int, double> dist, c_traffic_map, c_thermal_map,
                               c_os_map, c_contention_map, c_coherency_map;

        for (auto const& [id, _] : nodes) {
            dist[id]            = INF;
            c_traffic_map[id]   = 0.0;
            c_thermal_map[id]   = 0.0;
            c_os_map[id]        = 0.0;
            c_contention_map[id]= 0.0;
            c_coherency_map[id] = 0.0;
        }

        dist[start_id] = 0.0;
        pq.push({0.0, start_id});

        std::map<int, int> prev;

        // ── Arama Döngüsü ─────────────────────────────────────────────────────
        while (!pq.empty()) {
            auto [cur_dist, u] = pq.top(); pq.pop();
            if (u == end_id) break;
            if (cur_dist > dist[u]) continue;

            auto adj_it = adjacency_list.find(u);
            if (adj_it == adjacency_list.end()) continue;

            for (auto& edge : adj_it->second) {
                int v = edge.target_id;
                auto ni = nodes.find(v);
                if (ni == nodes.end()) continue;
                HardwareNode& tgt = ni->second;

                double w_base = edge.base_cycle_cost;

                // ── a) Bandwidth Saturation ───────────────────────────────────
                double c_traffic = edge.compute_traffic_penalty();

                // ── b) Contention Penalty ─────────────────────────────────────
                // Düğüm doluluk oranı %80 üzerindeyse eksponansiyel artış
                double c_contention = tgt.compute_contention_penalty(w_base);

                // ── c) Cache Coherency Penalty ────────────────────────────────
                // GPU/NE yazdı, CPU okumak istiyor → RAM'e gidip tazele
                double c_coherency = 0.0;
                if (tgt.is_dirty) {
                    c_coherency = COHERENCY_PENALTY;
                    // Coherency: veri yolu hem gidip hem gelmeli (+50 cycle bus)
                    c_coherency += 50.0;
                }

                // ── d) GC Kilidi ──────────────────────────────────────────────
                double c_gc = 0.0;
                if (tgt.gc_locked) {
                    c_gc = GC_LOCK_PENALTY;
                }

                // ── e) Thermal Throttling ─────────────────────────────────────
                // P_CORE ve GPU_CORE: sistem ≥90°C ise yol %50 yavaşlar
                double c_thermal = 0.0;
                if (state.thermal_throttling &&
                    (tgt.type == P_CORE || tgt.type == GPU_CORE)) {
                    c_thermal = w_base * 0.5;
                }
                // GPU özel throttling ≥85°C
                if (state.gpu_throttling && tgt.type == GPU_CORE) {
                    c_thermal += w_base * 0.25;
                }

                // ── f) OS Kısıtlamaları (Structural Hazard + QoS) ────────────
                double c_os = 0.0;
                if (tgt.is_busy && tgt.current_load >= tgt.max_capacity) {
                    if (task_qos > tgt.current_priority) {
                        // Preemption / Interrupt
                        c_os = CONTEXT_SWITCH_PENALTY;
                        state.total_context_switches++;
                        state.total_interrupts++;
                    } else {
                        // Queue delay — yolu fiilen kapatır
                        c_os = QUEUE_WAIT_PENALTY;
                    }
                }

                // ── g) Gevşetme (Relaxation) ──────────────────────────────────
                double w_final  = w_base + c_traffic + c_contention
                                + c_coherency + c_gc + c_thermal + c_os;
                double new_dist = cur_dist + w_final;

                if (new_dist < dist[v]) {
                    dist[v] = new_dist;
                    prev[v] = u;
                    c_traffic_map[v]    = c_traffic_map[u]    + c_traffic;
                    c_thermal_map[v]    = c_thermal_map[u]    + c_thermal;
                    c_os_map[v]         = c_os_map[u]         + c_os;
                    c_contention_map[v] = c_contention_map[u] + c_contention;
                    c_coherency_map[v]  = c_coherency_map[u]  + c_coherency;
                    pq.push({new_dist, v});
                }
            }
        }

        // ── Sonuç Derleme ─────────────────────────────────────────────────────
        if (dist[end_id] == INF) {
            result.route_found = false;
            return result;
        }

        // Yolu geri izle
        std::vector<int> path;
        for (int at = end_id; at != start_id; ) {
            path.push_back(at);
            auto p = prev.find(at);
            if (p == prev.end()) break;
            at = p->second;
        }
        path.push_back(start_id);
        std::reverse(path.begin(), path.end());

        // Rota üzerindeki trafik yükünü artır + is_dirty temizle
        bool hit_ssd = false, hit_miss = false;
        for (int i = 0; i + 1 < static_cast<int>(path.size()); i++) {
            increase_edge_traffic(path[i], path[i+1], 0.12);

            // Miss zinciri tespiti
            auto src_it = nodes.find(path[i]);
            auto dst_it = nodes.find(path[i+1]);
            if (src_it != nodes.end() && dst_it != nodes.end()) {
                NodeType src_t = src_it->second.type;
                NodeType dst_t = dst_it->second.type;

                // L1 → L2 geçişi = L1 miss
                if (src_t == L1_CACHE && dst_t == L2_CACHE) {
                    hit_miss = true;
                    state.cache_miss_count++;
                }
                // SLC → RAM = büyük miss / page fill
                if (src_t == SLC && dst_t == UNIFIED_RAM) {
                    state.cache_miss_count++;
                }
                // RAM → IO_HUB veya SSD = page fault / swap
                if ((src_t == UNIFIED_RAM || src_t == IO_HUB) &&
                    dst_t == NVME_SSD) {
                    hit_ssd = true;
                    state.page_fault_count++;
                }

                // Coherency temizle: CPU bu veriyi okudu
                if (dst_it->second.is_dirty) {
                    dst_it->second.is_dirty = false;
                }
            }
        }

        result.path               = path;
        result.total_cost         = dist[end_id];
        result.traffic_penalty    = c_traffic_map[end_id];
        result.thermal_penalty    = c_thermal_map[end_id];
        result.os_penalty         = c_os_map[end_id];
        result.contention_penalty = c_contention_map[end_id];
        result.coherency_penalty  = c_coherency_map[end_id];
        result.base_cost          = result.total_cost
                                    - result.traffic_penalty
                                    - result.thermal_penalty
                                    - result.os_penalty
                                    - result.contention_penalty
                                    - result.coherency_penalty;
        result.route_found         = true;
        result.triggered_page_fault= hit_ssd;
        result.triggered_cache_miss= hit_miss;

        // Global metrik güncelleme
        state.total_latency_sum  += result.total_cost;
        state.completed_routes++;
        state.total_cycles_elapsed += result.total_cost;

        return result;
    }
};  // class M2ProGraph


// ============================================================================
// BÖLÜM 5: PYBIND11 BINDING
// ============================================================================

PYBIND11_MODULE(m2pro_engine, m) {
    m.doc() =
        "Apple M2 Pro SoC Hardware-Software Co-Simulation Engine v2\n"
        "CPU (6P+4E) + GPU (19-core) + Neural Engine (16-core) + UMA\n"
        "Gelişmiş Dijkstra: Contention + Coherency + Thermal + OS penalties";

    // ── Enumlar ──────────────────────────────────────────────────────────────
    py::enum_<TaskPriority>(m, "TaskPriority")
        .value("BACKGROUND",     TaskPriority::BACKGROUND)
        .value("UTILITY",        TaskPriority::UTILITY)
        .value("USER_INITIATED", TaskPriority::USER_INITIATED)
        .value("INTERACTIVE",    TaskPriority::INTERACTIVE)
        .export_values();

    py::enum_<NodeType>(m, "NodeType")
        .value("P_CORE",        NodeType::P_CORE)
        .value("E_CORE",        NodeType::E_CORE)
        .value("ALU",           NodeType::ALU)
        .value("REGISTER_FILE", NodeType::REGISTER_FILE)
        .value("L1_CACHE",      NodeType::L1_CACHE)
        .value("L2_CACHE",      NodeType::L2_CACHE)
        .value("SLC",           NodeType::SLC)
        .value("UNIFIED_RAM",   NodeType::UNIFIED_RAM)
        .value("IO_HUB",        NodeType::IO_HUB)
        .value("NVME_SSD",      NodeType::NVME_SSD)
        .value("GPU_CORE",      NodeType::GPU_CORE)       // v2
        .value("NEURAL_ENGINE", NodeType::NEURAL_ENGINE)  // v2
        .export_values();

    // ── SystemState ──────────────────────────────────────────────────────────
    py::class_<SystemState>(m, "SystemState")
        .def(py::init<>())
        .def_readwrite("temperature",            &SystemState::temperature)
        .def_readwrite("gpu_temperature",        &SystemState::gpu_temperature)
        .def_readwrite("total_bus_load",         &SystemState::total_bus_load)
        .def_readwrite("thermal_throttling",     &SystemState::thermal_throttling)
        .def_readwrite("gpu_throttling",         &SystemState::gpu_throttling)
        .def_readwrite("gpu_memory_pressure",    &SystemState::gpu_memory_pressure)
        .def_readwrite("gc_active",              &SystemState::gc_active)
        .def_readwrite("total_context_switches", &SystemState::total_context_switches)
        .def_readwrite("total_interrupts",       &SystemState::total_interrupts)
        .def_readwrite("total_cycles_elapsed",   &SystemState::total_cycles_elapsed)
        .def_readwrite("cache_miss_count",       &SystemState::cache_miss_count)
        .def_readwrite("page_fault_count",       &SystemState::page_fault_count)
        .def_readwrite("completed_routes",       &SystemState::completed_routes)
        .def("update_temperature",     &SystemState::update_temperature,
             py::arg("cpu_temp"))
        .def("update_gpu_temperature", &SystemState::update_gpu_temperature,
             py::arg("g_temp"))
        .def("average_memory_latency", &SystemState::average_memory_latency)
        .def("reset_stats",            &SystemState::reset_stats);

    // ── RouteResult ──────────────────────────────────────────────────────────
    py::class_<RouteResult>(m, "RouteResult")
        .def(py::init<>())
        .def_readwrite("path",                &RouteResult::path)
        .def_readwrite("total_cost",          &RouteResult::total_cost)
        .def_readwrite("base_cost",           &RouteResult::base_cost)
        .def_readwrite("traffic_penalty",     &RouteResult::traffic_penalty)
        .def_readwrite("thermal_penalty",     &RouteResult::thermal_penalty)
        .def_readwrite("os_penalty",          &RouteResult::os_penalty)
        .def_readwrite("contention_penalty",  &RouteResult::contention_penalty)
        .def_readwrite("coherency_penalty",   &RouteResult::coherency_penalty)
        .def_readwrite("route_found",         &RouteResult::route_found)
        .def_readwrite("triggered_page_fault",&RouteResult::triggered_page_fault)
        .def_readwrite("triggered_cache_miss",&RouteResult::triggered_cache_miss);

    // ── M2ProGraph ────────────────────────────────────────────────────────────
    py::class_<M2ProGraph>(m, "M2ProGraph")
        .def(py::init<>())
        .def_readwrite("state", &M2ProGraph::state)

        // Topoloji
        .def("build_m2_pro_topology", &M2ProGraph::build_m2_pro_topology)
        .def("count_edges",           &M2ProGraph::count_edges)

        // Düğüm sorgulama
        .def("get_node_name",         &M2ProGraph::get_node_name,        py::arg("id"))
        .def("get_node_type",         &M2ProGraph::get_node_type,        py::arg("id"))
        .def("get_node_is_busy",      &M2ProGraph::get_node_is_busy,     py::arg("id"))
        .def("get_node_priority",     &M2ProGraph::get_node_priority,    py::arg("id"))
        .def("get_node_active_task_id",&M2ProGraph::get_node_active_task_id,
             py::arg("id"))
        .def("get_node_ready_queue_size",&M2ProGraph::get_node_ready_queue_size,
             py::arg("id"))
        .def("get_node_time_slice_used",&M2ProGraph::get_node_time_slice_used,
             py::arg("id"))
        .def("get_node_time_quantum", &M2ProGraph::get_node_time_quantum, py::arg("id"))
        .def("get_node_load_ratio",   &M2ProGraph::get_node_load_ratio,  py::arg("id"))
        .def("get_node_is_dirty",     &M2ProGraph::get_node_is_dirty,    py::arg("id"))
        .def("get_node_max_capacity", &M2ProGraph::get_node_max_capacity,py::arg("id"))
        .def("get_node_current_load", &M2ProGraph::get_node_current_load,py::arg("id"))
        .def("get_node_temperature",  &M2ProGraph::get_node_temperature, py::arg("id"))
        .def("get_all_node_ids",      &M2ProGraph::get_all_node_ids)
        .def("get_neighbors",         &M2ProGraph::get_neighbors,        py::arg("node_id"))
        .def("get_edge_traffic",      &M2ProGraph::get_edge_traffic,
             py::arg("source"), py::arg("target"))

        // Görev yönetimi
        .def("assign_task_to_node",   &M2ProGraph::assign_task_to_node,
             py::arg("node_id"), py::arg("priority"), py::arg("cycles"))
        .def("free_node",             &M2ProGraph::free_node,            py::arg("node_id"))
        .def("set_time_quantum",      &M2ProGraph::set_time_quantum,     py::arg("quantum"))
        .def("get_time_quantum",      &M2ProGraph::get_time_quantum)
        .def("add_load_to_node",      &M2ProGraph::add_load_to_node,
             py::arg("node_id"), py::arg("amount") = 1)
        .def("remove_load_from_node", &M2ProGraph::remove_load_from_node,
             py::arg("node_id"), py::arg("amount") = 1)

        // Cache coherency
        .def("mark_node_dirty",       &M2ProGraph::mark_node_dirty,      py::arg("node_id"))
        .def("mark_node_clean",       &M2ProGraph::mark_node_clean,      py::arg("node_id"))

        // Sıcaklık ve GC
        .def("set_node_temperature",  &M2ProGraph::set_node_temperature,
             py::arg("node_id"), py::arg("temp"))
        .def("set_gc_lock",           &M2ProGraph::set_gc_lock,
             py::arg("node_id"), py::arg("locked"))

        // Bandwidth
        .def("increase_edge_traffic", &M2ProGraph::increase_edge_traffic,
             py::arg("source"), py::arg("target"), py::arg("amount") = 0.1)
        .def("decay_all_traffic",     &M2ProGraph::decay_all_traffic,
             py::arg("decay_rate") = 0.04)

        // Simülasyon
        .def("tick_simulation",       &M2ProGraph::tick_simulation)
        .def("find_optimal_route",    &M2ProGraph::find_optimal_route,
             py::arg("start_id"), py::arg("end_id"), py::arg("task_qos"));
}
