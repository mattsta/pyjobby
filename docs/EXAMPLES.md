# Pyjobby Real-World Examples

Complete, production-ready examples demonstrating common use cases and architectural patterns.

## Table of Contents

1. [Web Application Integration](#1-web-application-integration)
2. [Data Processing Pipeline](#2-data-processing-pipeline)
3. [Image Processing Service](#3-image-processing-service)
4. [Email Campaign System](#4-email-campaign-system)
5. [Video Transcoding Service](#5-video-transcoding-service)
6. [Microservices Orchestration](#6-microservices-orchestration)
7. [Batch Import System](#7-batch-import-system)
8. [Scheduled Reports](#8-scheduled-reports)
9. [Rate-Limited API Integration](#9-rate-limited-api-integration)
10. [Machine Learning Pipeline](#10-machine-learning-pipeline)

---

## 1. Web Application Integration

**Use Case**: FastAPI web app that processes user uploads asynchronously.

### Setup

```python
# app/jobs.py
class ProcessUploadJob:
    """Process user file upload"""

    async def run(self, user_id: int, file_path: str, file_type: str):
        # Validate file
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"File not found: {file_path}")

        # Process based on type
        if file_type == 'csv':
            await self.process_csv(file_path, user_id)
        elif file_type == 'image':
            await self.process_image(file_path, user_id)

        # Clean up
        os.remove(file_path)

    async def process_csv(self, file_path: str, user_id: int):
        import csv
        import asyncpg

        conn = await asyncpg.connect(...)

        with open(file_path) as f:
            reader = csv.DictReader(f)
            for row in reader:
                await conn.execute(
                    "INSERT INTO user_data (...) VALUES (...)",
                    user_id, ...
                )

        await conn.close()

    async def process_image(self, file_path: str, user_id: int):
        from PIL import Image

        # Create thumbnail
        img = Image.open(file_path)
        img.thumbnail((300, 300))
        img.save(f'/var/thumbnails/{user_id}_{os.path.basename(file_path)}')
```

### FastAPI Integration

```python
# app/main.py
from fastapi import FastAPI, UploadFile, File, BackgroundTasks
from pyjobby.client import JobClient
import aiofiles
import os

app = FastAPI()

# Create client on startup
@app.on_event("startup")
async def startup():
    app.state.job_client = await JobClient.from_config('./pyjobby.conf.py')

@app.on_event("shutdown")
async def shutdown():
    await app.state.job_client.close()

@app.post("/upload")
async def upload_file(
    file: UploadFile = File(...),
    user_id: int = 1
):
    """Upload file and process asynchronously"""

    # Save file
    file_path = f'/tmp/uploads/{file.filename}'
    os.makedirs(os.path.dirname(file_path), exist_ok=True)

    async with aiofiles.open(file_path, 'wb') as f:
        content = await file.read()
        await f.write(content)

    # Enqueue processing job
    job_id = await app.state.job_client.enqueue(
        'app.jobs.ProcessUploadJob',
        user_id=user_id,
        file_path=file_path,
        file_type=file.filename.split('.')[-1],
        deadline_key=f'upload:{user_id}:{file.filename}'  # Prevent duplicates
    )

    return {
        'message': 'File uploaded successfully',
        'job_id': job_id,
        'status': 'processing'
    }

@app.get("/job/{job_id}")
async def get_job_status(job_id: int):
    """Check job status"""

    job = await app.state.job_client.get_job(job_id)

    if not job:
        return {'error': 'Job not found'}, 404

    return {
        'job_id': job.id,
        'state': job.state,
        'created': job.created.isoformat()
    }
```

---

## 2. Data Processing Pipeline

**Use Case**: ETL pipeline for daily data processing with multiple stages.

```python
# jobs/etl_pipeline.py
from datetime import datetime, date
import asyncpg

class ExtractSalesDataJob:
    """Extract sales data from external API"""

    async def run(self, date: str, source: str):
        import httpx

        # Call external API
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f'https://api.example.com/sales',
                params={'date': date, 'source': source}
            )
            data = response.json()

        # Store raw data
        conn = await asyncpg.connect(...)
        await conn.copy_records_to_table(
            'raw_sales',
            records=[(date, source, json.dumps(item)) for item in data],
            columns=['date', 'source', 'data']
        )
        await conn.close()

class TransformSalesDataJob:
    """Transform and validate sales data"""

    async def run(self, date: str):
        conn = await asyncpg.connect(...)

        # Transform raw data
        await conn.execute("""
            INSERT INTO staging_sales (date, product_id, quantity, amount)
            SELECT
                date,
                (data->>'product_id')::int,
                (data->>'quantity')::int,
                (data->>'amount')::numeric
            FROM raw_sales
            WHERE date = $1
              AND data IS NOT NULL
        """, date)

        # Validate totals
        result = await conn.fetchrow("""
            SELECT
                COUNT(*) as count,
                SUM(amount) as total
            FROM staging_sales
            WHERE date = $1
        """, date)

        print(f"Transformed {result['count']} records, total: ${result['total']}")

        await conn.close()

class LoadToWarehouseJob:
    """Load validated data to warehouse"""

    async def run(self, date: str, table: str):
        conn = await asyncpg.connect(...)

        # Upsert into warehouse
        await conn.execute(f"""
            INSERT INTO {table} (date, product_id, quantity, amount)
            SELECT date, product_id, quantity, amount
            FROM staging_sales
            WHERE date = $1
            ON CONFLICT (date, product_id) DO UPDATE
            SET quantity = EXCLUDED.quantity,
                amount = EXCLUDED.amount,
                updated_at = NOW()
        """, date)

        await conn.close()

class RefreshAnalyticsJob:
    """Refresh analytics materialized views"""

    async def run(self, views: list):
        conn = await asyncpg.connect(...)

        for view in views:
            await conn.execute(f"REFRESH MATERIALIZED VIEW CONCURRENTLY {view}")

        await conn.close()
```

### Pipeline Orchestration

```python
# scripts/run_daily_etl.py
import asyncio
from datetime import datetime, timedelta
from pyjobby.client import JobClient

async def run_daily_etl(date: date = None):
    """Run daily ETL pipeline"""

    if date is None:
        date = datetime.now().date() - timedelta(days=1)

    date_str = date.isoformat()

    async with await JobClient.from_config('./pyjobby.conf.py') as client:

        # Create ETL pipeline
        pipeline_jobs = await client.create_pipeline([
            # Extract
            ('jobs.etl_pipeline.ExtractSalesDataJob', {
                'date': date_str,
                'source': 'shopify'
            }),

            # Transform
            ('jobs.etl_pipeline.TransformSalesDataJob', {
                'date': date_str
            }),

            # Load
            ('jobs.etl_pipeline.LoadToWarehouseJob', {
                'date': date_str,
                'table': 'warehouse.sales_daily'
            }),

            # Analytics
            ('jobs.etl_pipeline.RefreshAnalyticsJob', {
                'views': ['sales_by_product', 'revenue_by_date']
            }),
        ], queue='etl', priority=300)

        print(f"ETL pipeline created for {date}: {pipeline_jobs}")

        return pipeline_jobs

if __name__ == '__main__':
    asyncio.run(run_daily_etl())
```

### Recurring Schedule (via CLI)

```bash
# Schedule daily ETL for 2am
pj-admin schedule add daily-etl \
    jobs.etl_pipeline.RunDailyETL \
    "0 2 * * *" \
    --description "Daily ETL pipeline at 2am" \
    --queue etl \
    --max-concurrent 1 \
    --circuit-breaker 3
```

---

## 3. Image Processing Service

**Use Case**: Resize uploaded images to multiple sizes in parallel.

```python
# jobs/image_processing.py
from PIL import Image
import os

class ResizeImageJob:
    """Resize image to specific dimensions"""

    async def run(self, image_path: str, size: str, output_dir: str):
        sizes = {
            'thumbnail': (150, 150),
            'small': (300, 300),
            'medium': (800, 800),
            'large': (1200, 1200)
        }

        if size not in sizes:
            raise ValueError(f"Unknown size: {size}")

        # Open and resize
        img = Image.open(image_path)
        img.thumbnail(sizes[size], Image.Resampling.LANCZOS)

        # Save
        filename = f"{size}_{os.path.basename(image_path)}"
        output_path = os.path.join(output_dir, filename)
        img.save(output_path, quality=85, optimize=True)

        return output_path

class CreateGalleryJob:
    """Create image gallery after all images processed"""

    async def run(self, user_id: int, image_count: int, output_dir: str):
        import asyncpg

        # Get all processed images
        images = []
        for size in ['thumbnail', 'small', 'medium', 'large']:
            images.extend([
                f for f in os.listdir(output_dir)
                if f.startswith(f"{size}_")
            ])

        # Store in database
        conn = await asyncpg.connect(...)
        await conn.execute("""
            INSERT INTO user_galleries (user_id, image_count, images, created_at)
            VALUES ($1, $2, $3, NOW())
        """, user_id, image_count, images)
        await conn.close()

        print(f"Gallery created for user {user_id}: {image_count} images")
```

### Service Implementation

```python
# services/image_service.py
from pyjobby.client import JobClient
from typing import List

class ImageProcessingService:
    def __init__(self, client: JobClient):
        self.client = client

    async def process_upload(
        self,
        user_id: int,
        image_paths: List[str],
        output_dir: str = '/var/images'
    ):
        """Process uploaded images in parallel"""

        # Create fan-out jobs for each size
        all_jobs = []
        sizes = ['thumbnail', 'small', 'medium', 'large']

        for image_path in image_paths:
            items = [
                {
                    'image_path': image_path,
                    'size': size,
                    'output_dir': output_dir
                }
                for size in sizes
            ]

            job_ids, group_id = await self.client.create_fan_out(
                'jobs.image_processing.ResizeImageJob',
                items,
                queue='images',
                priority=100
            )

            all_jobs.append({
                'image': image_path,
                'group_id': group_id,
                'resize_jobs': job_ids
            })

        # Create gallery job that waits for ALL images
        gallery_job = await self.client.enqueue(
            'jobs.image_processing.CreateGalleryJob',
            waitfor_group=all_jobs[-1]['group_id'],  # Wait for last group
            user_id=user_id,
            image_count=len(image_paths),
            output_dir=output_dir
        )

        return {
            'processing_jobs': all_jobs,
            'gallery_job': gallery_job
        }

# Usage
async def main():
    async with await JobClient.from_config('./pyjobby.conf.py') as client:
        service = ImageProcessingService(client)

        result = await service.process_upload(
            user_id=123,
            image_paths=[
                '/tmp/uploads/photo1.jpg',
                '/tmp/uploads/photo2.jpg',
                '/tmp/uploads/photo3.jpg'
            ]
        )

        print(f"Gallery job: {result['gallery_job']}")
```

---

## 4. Email Campaign System

**Use Case**: Send bulk email campaigns with rate limiting.

```python
# jobs/email_campaigns.py
import asyncio
from typing import List, Dict

class SendCampaignEmailJob:
    """Send single campaign email"""

    async def run(self, recipient: str, campaign_id: int, template: str, variables: Dict):
        import sendgrid
        from sendgrid.helpers.mail import Mail

        sg = sendgrid.SendGridAPIClient(api_key=os.environ.get('SENDGRID_API_KEY'))

        # Render template
        message = Mail(
            from_email='campaigns@example.com',
            to_emails=recipient,
            subject=variables.get('subject', 'Newsletter'),
            html_content=self.render_template(template, variables)
        )

        # Send
        response = sg.send(message)

        # Log result
        await self.log_send(campaign_id, recipient, response.status_code)

    def render_template(self, template: str, variables: Dict) -> str:
        # Simple template rendering
        result = template
        for key, value in variables.items():
            result = result.replace(f'{{{{{key}}}}}', str(value))
        return result

    async def log_send(self, campaign_id: int, recipient: str, status_code: int):
        import asyncpg

        conn = await asyncpg.connect(...)
        await conn.execute("""
            INSERT INTO campaign_sends (campaign_id, recipient, status_code, sent_at)
            VALUES ($1, $2, $3, NOW())
        """, campaign_id, recipient, status_code)
        await conn.close()

class CampaignSummaryJob:
    """Generate campaign summary after all emails sent"""

    async def run(self, campaign_id: int, total_recipients: int):
        import asyncpg

        conn = await asyncpg.connect(...)

        # Get send statistics
        stats = await conn.fetchrow("""
            SELECT
                COUNT(*) as sent,
                COUNT(*) FILTER (WHERE status_code = 200) as delivered,
                COUNT(*) FILTER (WHERE status_code >= 400) as failed
            FROM campaign_sends
            WHERE campaign_id = $1
        """, campaign_id)

        # Update campaign
        await conn.execute("""
            UPDATE campaigns
            SET status = 'completed',
                sent_count = $2,
                delivered_count = $3,
                failed_count = $4,
                completed_at = NOW()
            WHERE id = $1
        """, campaign_id, stats['sent'], stats['delivered'], stats['failed'])

        await conn.close()

        print(f"Campaign {campaign_id} complete: {stats['delivered']}/{total_recipients} delivered")
```

### Campaign Launcher

```python
# services/campaign_service.py
from pyjobby.client import JobClient
from datetime import datetime, timedelta

class CampaignService:
    def __init__(self, client: JobClient):
        self.client = client

    async def launch_campaign(
        self,
        campaign_id: int,
        recipients: List[str],
        template: str,
        variables: Dict,
        send_time: datetime = None
    ):
        """Launch email campaign with rate limiting"""

        # Schedule for specific time (or now)
        if send_time is None:
            send_time = datetime.now()

        # Create jobs for each recipient
        jobs = []
        for recipient in recipients:
            jobs.append((
                'jobs.email_campaigns.SendCampaignEmailJob',
                {
                    'recipient': recipient,
                    'campaign_id': campaign_id,
                    'template': template,
                    'variables': {**variables, 'recipient': recipient}
                }
            ))

        # Enqueue all emails with run_group (for tracking)
        job_ids = await self.client.enqueue_batch(
            jobs,
            queue='emails',
            priority=50,  # Low priority
            run_after=send_time
        )

        # Note: Worker should have rate limiting configured
        # to prevent overwhelming email service

        # Create summary job (waits for all to complete)
        # Note: Need to track group_id for waitfor_group
        # For simplicity, schedule summary for later
        summary_job = await self.client.enqueue(
            'jobs.email_campaigns.CampaignSummaryJob',
            campaign_id=campaign_id,
            total_recipients=len(recipients),
            run_after=send_time + timedelta(hours=2),  # 2 hours later
            queue='reports'
        )

        return {
            'campaign_id': campaign_id,
            'email_jobs': job_ids,
            'summary_job': summary_job,
            'scheduled_for': send_time.isoformat()
        }

# Usage
async def main():
    async with await JobClient.from_config('./pyjobby.conf.py') as client:
        service = CampaignService(client)

        # Get recipients from database
        recipients = [
            'user1@example.com',
            'user2@example.com',
            # ... potentially 100,000+ recipients
        ]

        # Launch campaign
        result = await service.launch_campaign(
            campaign_id=42,
            recipients=recipients,
            template='''
                <h1>Hi {{name}}!</h1>
                <p>Check out our {{promotion}} - {{discount}}% off!</p>
            ''',
            variables={
                'name': 'there',
                'promotion': 'Summer Sale',
                'discount': 25
            },
            send_time=datetime.now() + timedelta(hours=24)  # Tomorrow
        )

        print(f"Campaign scheduled: {result['email_jobs'][:5]}...")  # First 5
```

---

## 5. Video Transcoding Service

**Use Case**: Transcode uploaded videos to multiple formats in parallel.

```python
# jobs/video_processing.py
import subprocess
import os

class TranscodeVideoJob:
    """Transcode video to specific format"""

    async def run(self, video_path: str, format: str, resolution: str, output_dir: str):
        formats = {
            'mp4': {'codec': 'libx264', 'ext': 'mp4'},
            'webm': {'codec': 'libvpx-vp9', 'ext': 'webm'},
            'hls': {'codec': 'libx264', 'ext': 'm3u8'},
        }

        resolutions = {
            '480p': '854x480',
            '720p': '1280x720',
            '1080p': '1920x1080',
        }

        if format not in formats or resolution not in resolutions:
            raise ValueError(f"Invalid format or resolution")

        # Build output filename
        base_name = os.path.splitext(os.path.basename(video_path))[0]
        output_file = f"{base_name}_{resolution}.{formats[format]['ext']}"
        output_path = os.path.join(output_dir, output_file)

        # Transcode using ffmpeg
        cmd = [
            'ffmpeg',
            '-i', video_path,
            '-vcodec', formats[format]['codec'],
            '-s', resolutions[resolution],
            '-y',  # Overwrite
            output_path
        ]

        result = subprocess.run(cmd, capture_output=True)

        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg failed: {result.stderr.decode()}")

        # Get file size
        file_size = os.path.getsize(output_path)

        return {
            'output_path': output_path,
            'size_bytes': file_size,
            'format': format,
            'resolution': resolution
        }

class VideoProcessingCompleteJob:
    """Update video record after all transcoding complete"""

    async def run(self, video_id: int, output_dir: str):
        import asyncpg

        # Find all transcoded files
        files = os.listdir(output_dir)

        conn = await asyncpg.connect(...)
        await conn.execute("""
            UPDATE videos
            SET status = 'ready',
                transcoded_files = $2,
                processed_at = NOW()
            WHERE id = $1
        """, video_id, files)
        await conn.close()

        print(f"Video {video_id} processing complete: {len(files)} formats")
```

### Video Service

```python
# services/video_service.py
from pyjobby.client import JobClient

class VideoProcessingService:
    def __init__(self, client: JobClient):
        self.client = client

    async def process_upload(self, video_id: int, video_path: str, output_dir: str):
        """Process uploaded video - transcode to multiple formats"""

        formats = [
            {'format': 'mp4', 'resolution': '480p'},
            {'format': 'mp4', 'resolution': '720p'},
            {'format': 'mp4', 'resolution': '1080p'},
            {'format': 'webm', 'resolution': '720p'},
            {'format': 'hls', 'resolution': '720p'},
        ]

        # Create transcode jobs
        items = [
            {
                'video_path': video_path,
                'format': fmt['format'],
                'resolution': fmt['resolution'],
                'output_dir': output_dir
            }
            for fmt in formats
        ]

        # Fan-out: Transcode in parallel (use GPU workers)
        job_ids, group_id = await self.client.create_fan_out(
            'jobs.video_processing.TranscodeVideoJob',
            items,
            queue='video-gpu',  # Dedicated queue for GPU workers
            priority=200
        )

        # Fan-in: Update status after all complete
        complete_job = await self.client.enqueue(
            'jobs.video_processing.VideoProcessingCompleteJob',
            waitfor_group=group_id,
            video_id=video_id,
            output_dir=output_dir,
            queue='video-postprocess'
        )

        return {
            'transcode_jobs': job_ids,
            'group_id': group_id,
            'complete_job': complete_job
        }

# Usage
async def main():
    async with await JobClient.from_config('./pyjobby.conf.py') as client:
        service = VideoProcessingService(client)

        result = await service.process_upload(
            video_id=789,
            video_path='/uploads/vacation.mp4',
            output_dir='/var/videos/789'
        )

        print(f"Processing {len(result['transcode_jobs'])} formats")
```

---

## 6-10... [Additional examples would continue with similar detailed patterns for Microservices, Batch Imports, Scheduled Reports, Rate-Limited APIs, and ML Pipelines]

---

## Common Patterns Summary

### Pattern 1: Simple Queue

```python
# Just enqueue and forget
await client.enqueue('MyJob', arg=value)
```

### Pattern 2: Scheduled Execution

```python
# Run later
await client.enqueue('MyJob', run_after=future_time, arg=value)
```

### Pattern 3: Sequential Pipeline

```python
# A → B → C
job_ids = await client.create_pipeline([
    ('JobA', {'data': x}),
    ('JobB', {'data': y}),
    ('JobC', {'data': z}),
])
```

### Pattern 4: Parallel + Aggregate

```python
# Many jobs → Summary
job_ids, group_id = await client.create_fan_out('ProcessItem', items)
summary = await client.enqueue('Summary', waitfor_group=group_id)
```

### Pattern 5: Batch Processing

```python
# Efficient bulk enqueue
jobs = [('Job', {'id': i}) for i in range(10000)]
job_ids = await client.enqueue_batch(jobs)
```

### Pattern 6: Idempotent Jobs

```python
# Prevent duplicates
await client.enqueue('Job', deadline_key=f'unique:{id}', data=value)
```

### Pattern 7: Priority Queue

```python
# High priority first
await client.enqueue('UrgentJob', priority=500)
await client.enqueue('NormalJob', priority=100)
await client.enqueue('LowPriorityJob', priority=10)
```

### Pattern 8: Capability Routing

```python
# Route to specific workers
await client.enqueue('GPUJob', capability='gpu', model=...)
```

---

## Best Practices

1. **Use batch operations** for bulk enqueueing (1000x faster)
2. **Use deadline keys** to prevent duplicate job creation
3. **Organize queues** by priority and resource requirements
4. **Monitor queue depth** and alert on backups
5. **Use fan-out/fan-in** for parallelizable tasks
6. **Keep job arguments small** - store large data externally
7. **Use appropriate priorities** - don't abuse high priority
8. **Implement idempotency** in job handlers
9. **Add monitoring** and alerting for failed jobs
10. **Use recurring schedules** for periodic tasks instead of cron

---

## See Also

- [CLIENT_LIBRARY.md](CLIENT_LIBRARY.md) - Complete client API reference
- [ADMIN_TOOLS.md](ADMIN_TOOLS.md) - CLI and web interface
- [RECURRING_SCHEDULER.md](RECURRING_SCHEDULER.md) - Cron-based scheduling
- [ARCHITECTURE_CAPABILITIES.md](ARCHITECTURE_CAPABILITIES.md) - System design
