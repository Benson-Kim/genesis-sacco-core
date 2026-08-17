import 'package:hive_flutter/hive_flutter.dart';

/// Offline read cache for admin app (member lookups, arrears worklist).
class OfflineCache {
  static const _membersBox = 'gp_admin_members';
  static const _arrearsBox = 'gp_admin_arrears';

  static Future<void> init() async {
    await Hive.initFlutter();
    await Future.wait([
      Hive.openBox<String>(_membersBox),
      Hive.openBox<String>(_arrearsBox),
    ]);
  }

  Future<void> saveMember(String memberId, String jsonPayload) async {
    final box = Hive.box<String>(_membersBox);
    await box.put(memberId, jsonPayload);
  }

  String? readMember(String memberId) =>
      Hive.box<String>(_membersBox).get(memberId);

  Future<void> saveArrearsWorklist(String jsonPayload) async {
    final box = Hive.box<String>(_arrearsBox);
    await box.put('worklist', jsonPayload);
  }

  String? readArrearsWorklist() =>
      Hive.box<String>(_arrearsBox).get('worklist');

  Future<void> clearAll() async {
    await Future.wait([
      Hive.box<String>(_membersBox).clear(),
      Hive.box<String>(_arrearsBox).clear(),
    ]);
  }
}
