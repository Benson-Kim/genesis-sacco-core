/// Member-domain models — generated from OpenAPI spec.
/// Hand-written stub; regenerate with `scripts/regenerate_api_client.sh`
/// once the backend OpenAPI spec is published.
library;

enum MemberType { person, company, group, vehicle }

enum MemberStatus { active, arrears, exited }

class MemberSummary {
  const MemberSummary({
    required this.id,
    required this.memberNo,
    required this.fullName,
    required this.type,
    required this.status,
    required this.shareBalance,
    required this.depositBalance,
  });

  factory MemberSummary.fromJson(Map<String, dynamic> json) => MemberSummary(
        id: json['id'] as String,
        memberNo: json['member_no'] as String,
        fullName: json['full_name'] as String,
        type: MemberType.values.byName(json['type'] as String),
        status: MemberStatus.values.byName(json['status'] as String),
        shareBalance: json['share_balance'] as String,
        depositBalance: json['deposit_balance'] as String,
      );

  final String id;
  final String memberNo;
  final String fullName;
  final MemberType type;
  final MemberStatus status;

  /// KES amount as string (NUMERIC(18,2) from backend — never float).
  final String shareBalance;

  /// KES amount as string (NUMERIC(18,2) from backend — never float).
  final String depositBalance;
}

class PagedMembers {
  const PagedMembers({required this.items, this.nextCursor});

  factory PagedMembers.fromJson(Map<String, dynamic> json) => PagedMembers(
        items: (json['items'] as List<dynamic>)
            .map((e) => MemberSummary.fromJson(e as Map<String, dynamic>))
            .toList(),
        nextCursor: json['next_cursor'] as String?,
      );

  final List<MemberSummary> items;
  final String? nextCursor;
}
