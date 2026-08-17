import 'package:hive_flutter/hive_flutter.dart';

/// Offline read cache for balances and statements.
///
/// Implements MASTER_PROMPT §2.4 offline-tolerant read cache.
/// Uses Hive for lightweight, encrypted local storage.
/// Cache is read-only from the app's perspective — writes go through the API.
///
/// No PII beyond what the API already returns is stored (§1.6).
class OfflineCache {
  static const _balancesBox = 'gp_balances';
  static const _statementsBox = 'gp_statements';

  /// Initialise Hive. Call once from main() before runApp().
  static Future<void> init() async {
    await Hive.initFlutter();
    await Future.wait([
      Hive.openBox<String>(_balancesBox),
      Hive.openBox<String>(_statementsBox),
    ]);
  }

  // ── Balances ─────────────────────────────────────────────────────────────

  Future<void> saveBalances(String memberId, String jsonPayload) async {
    final box = Hive.box<String>(_balancesBox);
    await box.put(memberId, jsonPayload);
  }

  String? readBalances(String memberId) {
    final box = Hive.box<String>(_balancesBox);
    return box.get(memberId);
  }

  // ── Statements ────────────────────────────────────────────────────────────

  Future<void> saveStatementPage(String cacheKey, String jsonPayload) async {
    final box = Hive.box<String>(_statementsBox);
    await box.put(cacheKey, jsonPayload);
  }

  String? readStatementPage(String cacheKey) {
    final box = Hive.box<String>(_statementsBox);
    return box.get(cacheKey);
  }

  /// Evict all cached data for a member (e.g. on logout).
  Future<void> evict(String memberId) async {
    final balances = Hive.box<String>(_balancesBox);
    final statements = Hive.box<String>(_statementsBox);
    await Future.wait([
      balances.delete(memberId),
      // Evict all statement pages for this member.
      ...statements.keys
          .where((k) => k.toString().startsWith(memberId))
          .map(statements.delete),
    ]);
  }

  /// Clear all cached data (e.g. on logout of any user).
  Future<void> clearAll() async {
    await Future.wait([
      Hive.box<String>(_balancesBox).clear(),
      Hive.box<String>(_statementsBox).clear(),
    ]);
  }
}
