CREATE TABLE "issue_types" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"company_id" uuid NOT NULL,
	"key" text NOT NULL,
	"name" text NOT NULL,
	"description" text,
	"color" text NOT NULL,
	"icon" text,
	"field_definitions" jsonb DEFAULT '[]'::jsonb NOT NULL,
	"is_default" boolean DEFAULT false NOT NULL,
	"archived_at" timestamp with time zone,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	"updated_at" timestamp with time zone DEFAULT now() NOT NULL
);
--> statement-breakpoint
ALTER TABLE "issues" ADD COLUMN "issue_type_id" uuid;--> statement-breakpoint
ALTER TABLE "issues" ADD COLUMN "custom_fields" jsonb DEFAULT '{}'::jsonb NOT NULL;--> statement-breakpoint
ALTER TABLE "issue_types" ADD CONSTRAINT "issue_types_company_id_companies_id_fk" FOREIGN KEY ("company_id") REFERENCES "public"."companies"("id") ON DELETE cascade ON UPDATE no action;--> statement-breakpoint
CREATE INDEX "issue_types_company_idx" ON "issue_types" USING btree ("company_id");--> statement-breakpoint
CREATE UNIQUE INDEX "issue_types_company_key_idx" ON "issue_types" USING btree ("company_id","key");--> statement-breakpoint
CREATE UNIQUE INDEX "issue_types_company_default_idx" ON "issue_types" USING btree ("company_id") WHERE "issue_types"."is_default" = true and "issue_types"."archived_at" is null;--> statement-breakpoint
ALTER TABLE "issues" ADD CONSTRAINT "issues_issue_type_id_issue_types_id_fk" FOREIGN KEY ("issue_type_id") REFERENCES "public"."issue_types"("id") ON DELETE set null ON UPDATE no action;--> statement-breakpoint
CREATE INDEX "issues_company_issue_type_idx" ON "issues" USING btree ("company_id","issue_type_id");