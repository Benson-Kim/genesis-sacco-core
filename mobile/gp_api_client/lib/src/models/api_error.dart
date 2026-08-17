/// Standardised error envelope from the Genesis Prestige API.
/// Matches the backend global error envelope: {category, correlation_id}.
/// Never exposes stack traces (MASTER_PROMPT §1.6).
class ApiError implements Exception {
  const ApiError({
    required this.category,
    required this.correlationId,
    this.statusCode,
  });

  factory ApiError.fromJson(Map<String, dynamic> json) => ApiError(
        category: json['category'] as String? ?? 'unknown_error',
        correlationId: json['correlation_id'] as String? ?? '',
      );

  final String category;
  final String correlationId;
  final int? statusCode;

  @override
  String toString() => 'ApiError(category: $category, ref: $correlationId)';
}
